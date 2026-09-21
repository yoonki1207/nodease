"""Run one local UI, API, or mixed Nodease load-test scenario."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import platform
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Sequence

from tests.load.config import (
    DEFAULT_TARGET_HOST,
    load_runtime_manifest,
    validate_target_host,
)
from tests.load.scenario_contract import (
    classify_achieved_rate,
    minimum_pacing_workers,
)


LOAD_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = LOAD_DIR.parents[1]
DEFAULT_MANIFEST = LOAD_DIR / ".env.runtime.local.json"
REPORTS_DIR = LOAD_DIR / "reports"
UI_USERS = 25
LATENCY_BUDGET_SECONDS = 10.0
STOP_TIMEOUT_SECONDS = 75
API_PROFILES = {
    "average": 0.35,
    "peak": 1.0,
    "burst": 3.0,
}
_RUN_TIME_PATTERN = re.compile(r"^[1-9][0-9]*[smh]$")
_LOADGEN_RESULT_KEYS = frozenset(
    {
        "target_start_rate_rps",
        "achieved_start_rate_rps",
        "started_total",
        "classification",
    }
)


@dataclass(frozen=True, slots=True)
class RunPlan:
    scenario: Literal["ui", "api", "mixed"]
    api_profile: str
    host: str
    run_time: str
    locustfile: Path
    users: int
    spawn_rate: int
    ui_users: int
    api_workers: int
    target_rps: float | None


def default_run_time(api_profile: str) -> str:
    if api_profile not in API_PROFILES:
        raise ValueError("api_profile must be average, peak, or burst")
    return "1m" if api_profile == "burst" else "5m"


def sanitized_locust_environment(
    environment: Mapping[str, str],
) -> dict[str, str]:
    """Remove ambient proxy/distributed Locust controls from the child."""

    proxy_names = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
    sanitized = {
        name: value
        for name, value in environment.items()
        if not name.upper().startswith("LOCUST_")
        and name.upper() not in proxy_names
    }
    no_proxy = "127.0.0.1,localhost,::1"
    sanitized["NO_PROXY"] = no_proxy
    sanitized["no_proxy"] = no_proxy
    sanitized["LOCUST_MODE_MASTER"] = "false"
    sanitized["LOCUST_MODE_WORKER"] = "false"
    sanitized["LOCUST_PROCESSES"] = "0"
    return sanitized


def build_run_plan(
    *,
    scenario: str,
    api_profile: str,
    host: str,
    run_time: str,
) -> RunPlan:
    validated_host = validate_target_host(host)
    if scenario not in {"ui", "api", "mixed"}:
        raise ValueError("scenario must be ui, api, or mixed")
    if api_profile not in API_PROFILES:
        raise ValueError("api_profile must be average, peak, or burst")
    if not isinstance(run_time, str) or _RUN_TIME_PATTERN.fullmatch(run_time) is None:
        raise ValueError("run_time must use a positive Locust s/m/h duration")

    target_rps = API_PROFILES[api_profile]
    api_workers = minimum_pacing_workers(target_rps, LATENCY_BUDGET_SECONDS)
    if scenario == "ui":
        return RunPlan(
            scenario="ui",
            api_profile=api_profile,
            host=validated_host,
            run_time=run_time,
            locustfile=LOAD_DIR / "ui_locust.py",
            users=UI_USERS,
            spawn_rate=5,
            ui_users=UI_USERS,
            api_workers=0,
            target_rps=None,
        )
    if scenario == "api":
        return RunPlan(
            scenario="api",
            api_profile=api_profile,
            host=validated_host,
            run_time=run_time,
            locustfile=LOAD_DIR / "api_locust.py",
            users=api_workers,
            spawn_rate=min(api_workers, 15),
            ui_users=0,
            api_workers=api_workers,
            target_rps=target_rps,
        )
    return RunPlan(
        scenario="mixed",
        api_profile=api_profile,
        host=validated_host,
        run_time=run_time,
        locustfile=LOAD_DIR / "mixed_locust.py",
        users=UI_USERS + api_workers,
        spawn_rate=min(UI_USERS + api_workers, 20),
        ui_users=UI_USERS,
        api_workers=api_workers,
        target_rps=target_rps,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one provider-free local Nodease load-test scenario"
    )
    parser.add_argument("scenario", choices=("ui", "api", "mixed"))
    parser.add_argument(
        "--api-profile",
        choices=tuple(API_PROFILES),
        default="peak",
    )
    parser.add_argument("--host", default=DEFAULT_TARGET_HOST)
    parser.add_argument(
        "--run-time",
        help="Locust duration; defaults to 1m for burst and 5m otherwise",
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    return parser


def _write_metadata(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def empty_loadgen_result() -> dict[str, object]:
    """Return the explicit not-applicable result used by UI-only runs."""

    return {
        "target_start_rate_rps": None,
        "achieved_start_rate_rps": None,
        "started_total": None,
        "classification": None,
    }


def _validated_loadgen_result(
    payload: Mapping[str, object],
) -> dict[str, object]:
    if frozenset(payload) != _LOADGEN_RESULT_KEYS:
        raise ValueError("load-generator result fields are invalid")
    if all(payload[key] is None for key in _LOADGEN_RESULT_KEYS):
        return empty_loadgen_result()
    if any(payload[key] is None for key in _LOADGEN_RESULT_KEYS):
        raise ValueError("load-generator result cannot be partially null")

    target = payload["target_start_rate_rps"]
    achieved = payload["achieved_start_rate_rps"]
    started = payload["started_total"]
    classification = payload["classification"]
    if (
        isinstance(target, bool)
        or not isinstance(target, (int, float))
        or not math.isfinite(target)
        or target <= 0
        or isinstance(achieved, bool)
        or not isinstance(achieved, (int, float))
        or not math.isfinite(achieved)
        or achieved < 0
        or isinstance(started, bool)
        or not isinstance(started, int)
        or started < 0
        or classification not in {"valid", "invalid"}
    ):
        raise ValueError("load-generator result values are invalid")
    normalized = {
        "target_start_rate_rps": float(target),
        "achieved_start_rate_rps": float(achieved),
        "started_total": started,
        "classification": classification,
    }
    if classify_achieved_rate(
        normalized["target_start_rate_rps"],
        normalized["achieved_start_rate_rps"],
    ) != normalized["classification"]:
        raise ValueError("load-generator classification is inconsistent")
    return normalized


def _validate_loadgen_run_context(
    metadata: Mapping[str, object],
    loadgen: Mapping[str, object],
) -> None:
    scenario = metadata.get("scenario")
    expected_target = metadata.get("target_rps")
    actual_target = loadgen["target_start_rate_rps"]
    if scenario == "ui":
        if expected_target is not None or actual_target is not None:
            raise ValueError("UI load-generator result must be not applicable")
        return
    if scenario not in {"api", "mixed"}:
        raise ValueError("load-generator scenario is invalid")
    if (
        isinstance(expected_target, bool)
        or not isinstance(expected_target, (int, float))
        or not math.isfinite(expected_target)
        or expected_target <= 0
        or actual_target is None
        or float(actual_target) != float(expected_target)
    ):
        raise ValueError("load-generator target does not match the run plan")


def persist_loadgen_result(
    path: Path,
    payload: Mapping[str, object],
) -> None:
    """Atomically persist only the bounded request-start result schema."""

    _write_metadata(path, _validated_loadgen_result(payload))


def merge_loadgen_result_artifact(
    *,
    metadata_path: Path,
    metadata: Mapping[str, object],
    artifact_path: Path,
) -> dict[str, object]:
    """Merge a validated child result while preserving parent metadata."""

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("load-generator result must be a JSON object")
    validated = _validated_loadgen_result(payload)
    _validate_loadgen_run_context(metadata, validated)
    merged = {**metadata, "loadgen": validated}
    _write_metadata(metadata_path, merged)
    return merged


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = build_run_plan(
            scenario=args.scenario,
            api_profile=args.api_profile,
            host=args.host,
            run_time=args.run_time or default_run_time(args.api_profile),
        )
        manifest_path = args.manifest.resolve()
        load_runtime_manifest(manifest_path)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if importlib.util.find_spec("locust") is None:
        print(
            "error: Locust is not installed; install tests/load/requirements.txt",
            file=sys.stderr,
        )
        return 2

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_dir = REPORTS_DIR / f"{timestamp}_{plan.scenario}_{plan.api_profile}"
    report_dir.mkdir(parents=True, exist_ok=False)
    csv_prefix = report_dir / "locust"
    metadata_path = report_dir / "run_metadata.json"
    loadgen_result_path = report_dir / "loadgen_result.json"
    metadata: dict[str, object] = {
        **asdict(plan),
        "locustfile": str(plan.locustfile),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "machine": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "logical_cpu_count": os.cpu_count(),
        },
        "loadgen": empty_loadgen_result(),
        "result": "running",
    }
    _write_metadata(metadata_path, metadata)

    command = (
        sys.executable,
        "-m",
        "locust",
        "-f",
        str(plan.locustfile),
        "--headless",
        "--host",
        plan.host,
        "--users",
        str(plan.users),
        "--spawn-rate",
        str(plan.spawn_rate),
        "--run-time",
        plan.run_time,
        "--stop-timeout",
        str(STOP_TIMEOUT_SECONDS),
        "--reset-stats",
        "--csv",
        str(csv_prefix),
        "--csv-full-history",
        "--html",
        str(report_dir / "report.html"),
    )
    child_environment = sanitized_locust_environment(os.environ)
    child_environment.pop("LOAD_TEST_USER_PASSWORD", None)
    child_environment.pop("LOAD_TEST_AUTH_TOKEN", None)
    child_environment.update(
        {
            "LOAD_TEST_RUNTIME_MANIFEST": str(manifest_path),
            "LOAD_TEST_TARGET_HOST": plan.host,
            "LOAD_TEST_UI_USERS": str(plan.ui_users),
            "LOAD_TEST_API_WORKERS": str(plan.api_workers),
            "LOAD_TEST_API_TARGET_RPS": str(plan.target_rps or 0),
            "LOAD_TEST_API_LATENCY_BUDGET_SECONDS": str(
                LATENCY_BUDGET_SECONDS
            ),
            "LOAD_TEST_LOADGEN_RESULT_PATH": str(loadgen_result_path.resolve()),
        }
    )
    print(
        f"starting {plan.scenario}/{plan.api_profile}: "
        f"users={plan.users}, target_rps={plan.target_rps or 'n/a'}, "
        f"reports={report_dir}"
    )
    completed = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        env=child_environment,
        check=False,
    )
    metadata["finished_at"] = datetime.now(timezone.utc).isoformat()
    metadata["exit_code"] = completed.returncode
    metadata["result"] = "completed" if completed.returncode == 0 else "invalid_or_failed"
    try:
        merge_loadgen_result_artifact(
            metadata_path=metadata_path,
            metadata=metadata,
            artifact_path=loadgen_result_path,
        )
    except (OSError, ValueError, json.JSONDecodeError):
        final_exit_code = completed.returncode or 2
        metadata["exit_code"] = final_exit_code
        metadata["result"] = "invalid_or_failed"
        _write_metadata(metadata_path, metadata)
        print(
            "error: load-generator result artifact is missing or invalid",
            file=sys.stderr,
        )
        return final_exit_code
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
