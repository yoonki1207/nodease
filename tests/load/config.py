"""Pure configuration contracts shared by the local load-test scenarios."""

from __future__ import annotations

import ipaddress
import json
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit
from uuid import UUID


_EXPECTED_UI_USERS = 25
_DEPLOYMENT_SLUG_PATTERN = re.compile(r"^[a-z0-9-]+$")
DEFAULT_TARGET_HOST = "http://127.0.0.1:18080"


class LoadTestConfigError(ValueError):
    """Safe configuration error that never includes manifest field values."""


@dataclass(frozen=True, slots=True)
class UiRuntimeUser:
    email: str
    workflow_id: UUID
    password: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ApiRuntimeBinding:
    workflow_id: UUID
    deployment_slug: str
    auth_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RuntimeManifest:
    schema_version: int
    organization_id: UUID
    ui_users: tuple[UiRuntimeUser, ...]
    api: ApiRuntimeBinding


class _StrictJsonError(ValueError):
    pass


def _reject_json_constant(_value: str) -> NoReturn:
    raise _StrictJsonError


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _StrictJsonError
        result[key] = value
    return result


def _require_exact_keys(
    value: Any,
    expected: frozenset[str],
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != expected:
        raise LoadTestConfigError(f"load-test manifest {label} shape is invalid")
    return value


def _require_uuid(value: Any, *, label: str) -> UUID:
    if not isinstance(value, str):
        raise LoadTestConfigError(f"load-test manifest {label} is invalid")
    try:
        return UUID(value)
    except (ValueError, AttributeError):
        raise LoadTestConfigError(
            f"load-test manifest {label} is invalid"
        ) from None


def _require_nonempty_string(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LoadTestConfigError(f"load-test manifest {label} is invalid")
    return value


def validate_target_host(host: str) -> str:
    """Return an explicit HTTP(S) loopback origin or fail closed."""

    if not isinstance(host, str) or not host or host != host.strip():
        raise LoadTestConfigError("load-test target host is invalid")
    try:
        parsed = urlsplit(host)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        raise LoadTestConfigError("load-test target host is invalid") from None

    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise LoadTestConfigError("load-test target host is invalid")

    if hostname.casefold() == "localhost":
        return host
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        raise LoadTestConfigError("load-test target must be loopback") from None
    if not address.is_loopback:
        raise LoadTestConfigError("load-test target must be loopback")
    return host


def load_runtime_manifest(path: str | Path) -> RuntimeManifest:
    """Load and strictly validate a secret-bearing local runtime manifest."""

    manifest_path = Path(path)
    try:
        mode = stat.S_IMODE(manifest_path.stat().st_mode)
        if mode & 0o077:
            raise LoadTestConfigError("load-test manifest permissions are unsafe")
        raw_text = manifest_path.read_text(encoding="utf-8")
    except LoadTestConfigError:
        raise
    except (OSError, UnicodeError):
        raise LoadTestConfigError("load-test manifest is unavailable") from None
    try:
        raw = json.loads(
            raw_text,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, _StrictJsonError):
        raise LoadTestConfigError("load-test manifest JSON is invalid") from None

    root = _require_exact_keys(
        raw,
        frozenset({"schema_version", "organization_id", "ui_users", "api"}),
        label="root",
    )
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise LoadTestConfigError("load-test manifest schema version is invalid")
    organization_id = _require_uuid(
        root["organization_id"], label="organization_id"
    )

    raw_ui_users = root["ui_users"]
    if not isinstance(raw_ui_users, list) or len(raw_ui_users) != _EXPECTED_UI_USERS:
        raise LoadTestConfigError("load-test manifest UI user count is invalid")

    ui_users: list[UiRuntimeUser] = []
    normalized_emails: set[str] = set()
    workflow_ids: set[UUID] = set()
    for raw_user in raw_ui_users:
        user = _require_exact_keys(
            raw_user,
            frozenset({"email", "password", "workflow_id"}),
            label="UI user",
        )
        email = _require_nonempty_string(user["email"], label="UI user email")
        normalized_email = email.strip().casefold()
        if "@" not in normalized_email or normalized_email in normalized_emails:
            raise LoadTestConfigError("load-test manifest UI user email is invalid")
        password = _require_nonempty_string(
            user["password"], label="UI user password"
        )
        workflow_id = _require_uuid(
            user["workflow_id"], label="UI user workflow_id"
        )
        if workflow_id in workflow_ids:
            raise LoadTestConfigError(
                "load-test manifest UI workflow binding is ambiguous"
            )
        normalized_emails.add(normalized_email)
        workflow_ids.add(workflow_id)
        ui_users.append(
            UiRuntimeUser(
                email=normalized_email,
                password=password,
                workflow_id=workflow_id,
            )
        )

    raw_api = _require_exact_keys(
        root["api"],
        frozenset({"workflow_id", "deployment_slug", "auth_token"}),
        label="API",
    )
    api_workflow_id = _require_uuid(
        raw_api["workflow_id"], label="API workflow_id"
    )
    if api_workflow_id in workflow_ids:
        raise LoadTestConfigError(
            "load-test manifest API workflow must be separate"
        )
    deployment_slug = _require_nonempty_string(
        raw_api["deployment_slug"], label="API deployment_slug"
    )
    if (
        len(deployment_slug) > 255
        or _DEPLOYMENT_SLUG_PATTERN.fullmatch(deployment_slug) is None
    ):
        raise LoadTestConfigError(
            "load-test manifest API deployment_slug is invalid"
        )
    auth_token = _require_nonempty_string(
        raw_api["auth_token"], label="API auth_token"
    )

    return RuntimeManifest(
        schema_version=1,
        organization_id=organization_id,
        ui_users=tuple(ui_users),
        api=ApiRuntimeBinding(
            workflow_id=api_workflow_id,
            deployment_slug=deployment_slug,
            auth_token=auth_token,
        ),
    )


__all__ = [
    "ApiRuntimeBinding",
    "LoadTestConfigError",
    "RuntimeManifest",
    "UiRuntimeUser",
    "load_runtime_manifest",
    "validate_target_host",
]
