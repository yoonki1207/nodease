from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import pytest

from tests.load.config import (
    LoadTestConfigError,
    load_runtime_manifest,
    validate_target_host,
)


UI_USER_COUNT = 25
PASSWORD_CANARY = "load-test-password-must-not-leak"
TOKEN_CANARY = "load-test-token-must-not-leak"


def _runtime_manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "organization_id": "00000000-0000-4000-8000-000000000001",
        "ui_users": [
            {
                "email": f"load-user-{index:02d}@example.test",
                "password": f"{PASSWORD_CANARY}-{index:02d}",
                "workflow_id": f"00000000-0000-4000-8000-{index + 100:012d}",
            }
            for index in range(UI_USER_COUNT)
        ],
        "api": {
            "workflow_id": "00000000-0000-4000-8000-000000000999",
            "deployment_slug": "provider-free-load-test",
            "auth_token": TOKEN_CANARY,
        },
    }


def _write_manifest(tmp_path: Path, manifest: dict[str, object]) -> Path:
    path = tmp_path / "runtime-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


@pytest.mark.parametrize(
    "host",
    (
        "http://localhost:8000",
        "https://localhost:8443",
        "http://127.0.0.1:8000",
        "http://127.42.0.9:8000",
        "http://[::1]:8000",
    ),
)
def test_load_target_accepts_only_explicit_loopback_hosts(host: str) -> None:
    assert validate_target_host(host) == host


@pytest.mark.parametrize(
    "host",
    (
        "https://moduly-ai.cloud",
        "http://192.168.0.10:8000",
        "http://0.0.0.0:8000",
        "http://localhost.example.com:8000",
        "http://user:password@localhost:8000",
        "ftp://localhost:8000",
    ),
)
def test_load_target_rejects_remote_userinfo_and_non_http_hosts(host: str) -> None:
    with pytest.raises(LoadTestConfigError):
        validate_target_host(host)


def test_runtime_manifest_requires_25_unique_ui_users_and_separate_api_workflow(
    tmp_path: Path,
) -> None:
    manifest = load_runtime_manifest(_write_manifest(tmp_path, _runtime_manifest()))

    assert manifest.schema_version == 1
    assert manifest.organization_id == UUID(
        "00000000-0000-4000-8000-000000000001"
    )
    assert len(manifest.ui_users) == UI_USER_COUNT
    assert len({user.email for user in manifest.ui_users}) == UI_USER_COUNT
    assert len({user.workflow_id for user in manifest.ui_users}) == UI_USER_COUNT
    assert manifest.api.workflow_id not in {
        user.workflow_id for user in manifest.ui_users
    }


@pytest.mark.parametrize(
    "invalid_case",
    (
        "too_few_ui_users",
        "duplicate_ui_email",
        "duplicate_ui_workflow",
        "api_reuses_ui_workflow",
    ),
)
def test_runtime_manifest_rejects_ambiguous_user_and_workflow_bindings(
    tmp_path: Path,
    invalid_case: str,
) -> None:
    raw = deepcopy(_runtime_manifest())
    ui_users = raw["ui_users"]
    api = raw["api"]
    assert isinstance(ui_users, list)
    assert isinstance(api, dict)

    if invalid_case == "too_few_ui_users":
        ui_users.pop()
    elif invalid_case == "duplicate_ui_email":
        ui_users[1]["email"] = ui_users[0]["email"]
    elif invalid_case == "duplicate_ui_workflow":
        ui_users[1]["workflow_id"] = ui_users[0]["workflow_id"]
    else:
        api["workflow_id"] = ui_users[0]["workflow_id"]

    with pytest.raises(LoadTestConfigError):
        load_runtime_manifest(_write_manifest(tmp_path, raw))


def test_runtime_manifest_errors_never_expose_password_or_api_token(
    tmp_path: Path,
) -> None:
    raw = _runtime_manifest()
    ui_users = raw["ui_users"]
    api = raw["api"]
    assert isinstance(ui_users, list)
    assert isinstance(api, dict)
    api["workflow_id"] = ui_users[0]["workflow_id"]

    with pytest.raises(LoadTestConfigError) as caught:
        load_runtime_manifest(_write_manifest(tmp_path, raw))

    rendered_error = f"{caught.value!s}\n{caught.value!r}"
    assert PASSWORD_CANARY not in rendered_error
    assert TOKEN_CANARY not in rendered_error


def test_runtime_manifest_rejects_group_or_world_access(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, _runtime_manifest())
    os.chmod(path, 0o644)

    with pytest.raises(LoadTestConfigError):
        load_runtime_manifest(path)
