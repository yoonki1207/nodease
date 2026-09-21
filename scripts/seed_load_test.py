"""Prepare and verify the dedicated local Nodease load-test profile."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from apps.shared.db.load_test_seed import (  # noqa: E402
    LoadTestSeedError,
    LoadTestSeedPlan,
    build_load_test_seed_plan,
    prepare_load_test_data,
    redacted_seed_summary,
    verify_load_test_data,
)


USER_PASSWORD_ENV = "LOAD_TEST_USER_PASSWORD"
API_AUTH_TOKEN_ENV = "LOAD_TEST_AUTH_TOKEN"


def resolve_seed_secrets(environment: Mapping[str, str]) -> tuple[str, str]:
    """Resolve required secrets using fixed, non-sensitive error messages."""

    user_password = environment.get(USER_PASSWORD_ENV)
    if not isinstance(user_password, str) or not user_password:
        raise RuntimeError(f"{USER_PASSWORD_ENV} is required")
    api_auth_token = environment.get(API_AUTH_TOKEN_ENV)
    if not isinstance(api_auth_token, str) or not api_auth_token:
        raise RuntimeError(f"{API_AUTH_TOKEN_ENV} is required")
    return user_password, api_auth_token


def runtime_manifest_payload(
    *,
    plan: LoadTestSeedPlan,
    user_password: str,
    api_auth_token: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "organization_id": str(plan.organization_id),
        "ui_users": [
            {
                "email": spec.email,
                "password": user_password,
                "workflow_id": str(spec.workflow_id),
            }
            for spec in plan.ui_users
        ],
        "api": {
            "workflow_id": str(plan.api_workflow_id),
            "deployment_slug": plan.api_slug,
            "auth_token": api_auth_token,
        },
    }


def write_runtime_manifest(
    path: str | Path,
    *,
    plan: LoadTestSeedPlan,
    user_password: str,
    api_auth_token: str,
) -> None:
    """Atomically write the secret-bearing Locust manifest with mode 0600."""

    destination = Path(path).resolve()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = runtime_manifest_payload(
        plan=plan,
        user_password=user_password,
        api_auth_token=api_auth_token,
    )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.chmod(temporary_path, 0o600)
            json.dump(payload, temporary, ensure_ascii=False, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, destination)
        os.chmod(destination, 0o600)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seed the isolated provider-free Nodease load-test profile"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("prepare", help="Idempotently prepare load-test rows")
    subparsers.add_parser("verify", help="Verify all load-test bindings and secrets")
    subparsers.add_parser("plan", help="Print the non-sensitive deterministic plan")
    manifest = subparsers.add_parser(
        "manifest", help="Write the private Locust runtime manifest"
    )
    manifest.add_argument("--output", type=Path, required=True)
    return parser


def _run_db_command(command: str, *, user_password: str, api_auth_token: str):
    from apps.shared.db.session import SessionLocal

    import apps.shared.db.models  # noqa: F401

    db = SessionLocal()
    try:
        if command == "prepare":
            plan = prepare_load_test_data(
                db,
                user_password=user_password,
                api_auth_token=api_auth_token,
            )
            db.commit()
            return redacted_seed_summary(plan)
        return verify_load_test_data(
            db,
            user_password=user_password,
            api_auth_token=api_auth_token,
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    selected_environment = environment if environment is not None else os.environ
    plan = build_load_test_seed_plan()

    try:
        if args.command == "plan":
            summary = redacted_seed_summary(plan)
        else:
            user_password, api_auth_token = resolve_seed_secrets(
                selected_environment
            )
            if args.command == "manifest":
                write_runtime_manifest(
                    args.output,
                    plan=plan,
                    user_password=user_password,
                    api_auth_token=api_auth_token,
                )
                summary = {
                    **redacted_seed_summary(plan),
                    "manifest": str(args.output),
                }
            else:
                summary = _run_db_command(
                    args.command,
                    user_password=user_password,
                    api_auth_token=api_auth_token,
                )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    except (LoadTestSeedError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(
            f"error: load-test seed failed ({type(error).__name__})",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
