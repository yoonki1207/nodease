"""Safely alternate the local Docker Compose stack for load testing.

The orchestration seam in :func:`run_action` is intentionally free of direct
subprocess calls. The command-line adapter performs read-only ownership
discovery first, then injects the only command runner that can mutate Docker.
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path


PROJECT_NAME = "nodease-loadtest"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPOSITORY_ROOT / "docker" / "docker-compose.yml"
COMPOSE_OVERRIDE_FILE = (
    REPOSITORY_ROOT / "tests" / "load" / "docker-compose.override.yml"
)
ENV_FILE = REPOSITORY_ROOT / "tests" / "load" / ".env.load.local"
RUNTIME_MANIFEST = (
    REPOSITORY_ROOT / "tests" / "load" / ".env.runtime.local.json"
)
DEV_RUNTIME_LOCK = REPOSITORY_ROOT / ".nodease-dev.lock"

FIXED_CONTAINER_NAMES = (
    "moduly-postgres",
    "moduly-redis",
    "moduly-gateway",
    "moduly-knowledge-worker",
    "moduly-workflow-engine",
    "moduly-log-system",
    "moduly-log-system-beat",
    "moduly-frontend",
    "moduly-sandbox",
    "moduly-nginx",
    "moduly-proxy",
)

UP_SERVICES = (
    "nginx",
    "workflow_engine",
    "log_system",
    "log_system_beat",
)

CommandRunner = Callable[[Sequence[str]], int]
Emitter = Callable[[str], None]

_SAFE_HOST_ENVIRONMENT_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "TERM",
        "COLORTERM",
    }
)
_LOAD_ENVIRONMENT_NAMES = frozenset(
    {
        "NODE_ENV",
        "STORAGE_TYPE",
        "SECRET_KEY",
        "MASTER_KEY",
        "ENCRYPTION_KEY",
        "LOAD_TEST_USER_PASSWORD",
        "LOAD_TEST_AUTH_TOKEN",
    }
)


class ComposeActionError(RuntimeError):
    """A safe, user-facing load-test Compose action failure."""


class ForeignContainerOwnerError(ComposeActionError):
    """A fixed container name belongs to another Compose project."""


class EnvironmentFileExistsError(ComposeActionError):
    """The private load environment already exists and must not be overwritten."""


class EnvironmentFileMissingError(ComposeActionError):
    """The private load environment has not been initialized."""


class DestructiveConfirmationError(ComposeActionError):
    """An isolated-volume deletion was not confirmed exactly."""


def require_destroy_confirmation(value: str | None) -> None:
    if value != PROJECT_NAME:
        raise DestructiveConfirmationError(
            f"destroy-data requires --confirm {PROJECT_NAME}"
        )


def _fernet_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def initialize_environment_file(path: Path, *, emit: Emitter) -> None:
    """Create a private, local-only environment without printing its values."""

    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    values = {
        "NODE_ENV": "development",
        "STORAGE_TYPE": "LOCAL",
        "SECRET_KEY": secrets.token_urlsafe(48),
        "MASTER_KEY": _fernet_key(),
        "ENCRYPTION_KEY": _fernet_key(),
        "LOAD_TEST_USER_PASSWORD": secrets.token_urlsafe(24),
        "LOAD_TEST_AUTH_TOKEN": "nodease_app_" + secrets.token_urlsafe(32),
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(destination, flags, 0o600)
    except FileExistsError:
        raise EnvironmentFileExistsError(
            "the private load-test environment already exists"
        ) from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(
                "# Generated local-only Nodease load-test environment.\n"
                "# Never commit or share this file.\n"
            )
            for name, value in values.items():
                stream.write(f"{name}={value}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(destination, 0o600)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    emit(f"created private load-test environment: {destination}")


def load_environment_file(path: Path) -> dict[str, str]:
    """Read the simple generated KEY=VALUE file without exposing its values."""

    try:
        if path.stat().st_mode & 0o077:
            raise ComposeActionError(
                "the private load-test environment permissions are unsafe"
            )
        lines = path.read_text(encoding="utf-8").splitlines()
    except ComposeActionError:
        raise
    except OSError:
        raise EnvironmentFileMissingError(
            "run the init action before using the load-test stack"
        ) from None
    result: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, value = stripped.partition("=")
        if not separator or not name or name in result:
            raise ComposeActionError("the private load-test environment is invalid")
        result[name] = value
    return result


def sanitized_subprocess_environment(
    environment: Mapping[str, str],
) -> dict[str, str]:
    """Remove ambient Compose control variables from a subprocess environment."""

    return {
        name: value
        for name, value in environment.items()
        if name in _SAFE_HOST_ENVIRONMENT_NAMES
        or name in _LOAD_ENVIRONMENT_NAMES
        or name.startswith("LC_")
    }


def _secret_values(environment: Mapping[str, str]) -> tuple[str, ...]:
    markers = (
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "CREDENTIAL",
        "ENCRYPTION_KEY",
        "MASTER_KEY",
    )
    values = {
        value
        for name, value in environment.items()
        if value
        and len(value) >= 6
        and any(marker in name.upper() for marker in markers)
    }
    return tuple(sorted(values, key=len, reverse=True))


def redact_secrets(message: str, environment: Mapping[str, str]) -> str:
    """Redact known secret values without printing or previewing them."""

    redacted = message
    for value in _secret_values(environment):
        redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def _compose_prefix() -> tuple[str, ...]:
    return (
        "docker",
        "--context",
        "default",
        "compose",
        "--env-file",
        str(ENV_FILE),
        "--project-name",
        PROJECT_NAME,
        "--project-directory",
        str(COMPOSE_FILE.parent),
        "--file",
        str(COMPOSE_FILE),
        "--file",
        str(COMPOSE_OVERRIDE_FILE),
    )


def _action_commands(action: str) -> tuple[tuple[str, ...], ...]:
    prefix = _compose_prefix()
    if action == "doctor":
        return (prefix + ("config", "--quiet"),)
    if action == "up":
        return (
            prefix
            + (
                "up",
                "--detach",
                "--build",
                "--wait",
                *UP_SERVICES,
            ),
        )
    if action == "down":
        # Deliberately omit --volumes/-v. Load-test data survives normal stops.
        return (prefix + ("down",),)
    if action == "destroy-data":
        return (prefix + ("down", "--volumes"),)
    if action == "status":
        return (prefix + ("ps",),)
    if action in {"seed", "verify"}:
        seed_command = "prepare" if action == "seed" else "verify"
        return (
            prefix
            + (
                "exec",
                "--no-tty",
                "-e",
                "LOAD_TEST_USER_PASSWORD",
                "-e",
                "LOAD_TEST_AUTH_TOKEN",
                "gateway",
                "python",
                "/app/scripts/seed_load_test.py",
                seed_command,
            ),
        )
    raise ValueError(f"unsupported action: {action}")


def _assert_safe_owners(container_owners: Mapping[str, str | None]) -> None:
    foreign = {
        name: owner or "unmanaged"
        for name, owner in container_owners.items()
        if name in FIXED_CONTAINER_NAMES and owner != PROJECT_NAME
    }
    if not foreign:
        return

    details = ", ".join(
        f"{name} (project={owner})" for name, owner in sorted(foreign.items())
    )
    raise ForeignContainerOwnerError(
        "fixed container names are already owned by another project: " + details
    )


def run_action(
    action: str,
    *,
    container_owners: Mapping[str, str | None],
    environment: Mapping[str, str],
    run_command: CommandRunner,
    emit: Emitter,
) -> int:
    """Plan and run one action through injected, testable I/O boundaries."""

    if action in {"doctor", "up", "down", "destroy-data", "seed", "verify"}:
        _assert_safe_owners(container_owners)

    try:
        for command in _action_commands(action):
            return_code = run_command(command)
            if return_code != 0:
                raise ComposeActionError(
                    f"Docker Compose action {action!r} exited with {return_code}"
                )
    except ForeignContainerOwnerError:
        raise
    except Exception as error:
        safe_message = redact_secrets(str(error), environment)
        raise ComposeActionError(safe_message) from None

    emit(f"{action} completed for Compose project {PROJECT_NAME}")
    return 0


def _inspect_container_owners(
    environment: Mapping[str, str],
) -> dict[str, str | None]:
    command = (
        "docker",
        "--context",
        "default",
        "container",
        "ls",
        "--all",
        "--format",
        '{{.Names}}\t{{.Label "com.docker.compose.project"}}',
    )
    completed = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        env=sanitized_subprocess_environment(environment),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "Docker container discovery failed"
        raise ComposeActionError(redact_secrets(detail, environment))

    owners: dict[str, str | None] = {}
    for line in completed.stdout.splitlines():
        name, separator, owner = line.partition("\t")
        if name not in FIXED_CONTAINER_NAMES:
            continue
        owners[name] = owner.strip() if separator and owner.strip() else None
    return owners


def _streaming_runner(environment: Mapping[str, str], emit: Emitter) -> CommandRunner:
    subprocess_environment = sanitized_subprocess_environment(environment)

    def run(command: Sequence[str]) -> int:
        process = subprocess.Popen(
            tuple(command),
            cwd=REPOSITORY_ROOT,
            env=subprocess_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert process.stdout is not None
        for line in process.stdout:
            emit(redact_secrets(line.rstrip("\n"), environment))
        return process.wait()

    return run


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the isolated Nodease Docker Compose load-test project"
    )
    parser.add_argument(
        "action",
        choices=(
            "init",
            "doctor",
            "up",
            "seed",
            "verify",
            "down",
            "destroy-data",
            "status",
        ),
    )
    parser.add_argument(
        "--confirm",
        help=f"Required only for destroy-data; must equal {PROJECT_NAME}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    environment = dict(os.environ)

    try:
        if args.action == "init":
            initialize_environment_file(ENV_FILE, emit=print)
            return 0
        if args.action == "destroy-data":
            require_destroy_confirmation(args.confirm)
        environment.update(load_environment_file(ENV_FILE))
        if args.action == "up" and DEV_RUNTIME_LOCK.exists():
            raise ForeignContainerOwnerError(
                "the host development runtime is active; stop scripts/dev.sh first"
            )
        owners = _inspect_container_owners(environment)
        return_code = run_action(
            args.action,
            container_owners=owners,
            environment=environment,
            run_command=_streaming_runner(environment, print),
            emit=print,
        )
        if args.action == "seed":
            if str(REPOSITORY_ROOT) not in sys.path:
                sys.path.insert(0, str(REPOSITORY_ROOT))
            from apps.shared.db.load_test_seed import build_load_test_seed_plan
            from scripts.seed_load_test import write_runtime_manifest

            write_runtime_manifest(
                RUNTIME_MANIFEST,
                plan=build_load_test_seed_plan(),
                user_password=environment["LOAD_TEST_USER_PASSWORD"],
                api_auth_token=environment["LOAD_TEST_AUTH_TOKEN"],
            )
            print(f"created private runtime manifest: {RUNTIME_MANIFEST}")
        return return_code
    except Exception as error:
        safe_message = redact_secrets(str(error), environment)
        print(f"error: {safe_message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
