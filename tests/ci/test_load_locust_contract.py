from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.load.run_scenario import (
    STOP_TIMEOUT_SECONDS,
    build_run_plan,
    default_run_time,
    sanitized_locust_environment,
)
from tests.load.scenario_common import (
    RuntimeUserPool,
    ScenarioDataError,
    build_draft_save_payload,
    deployment_result_error,
    execution_result_error,
    pacing_wait_seconds,
)
from tests.load.scenario_contract import (
    LoadStartupTracker,
    REQUEST_TIMEOUT_SECONDS,
    RequestStartRateTracker,
)


LOAD_DIR = Path(__file__).resolve().parents[1] / "load"


@pytest.mark.parametrize(
    ("profile", "target_rps", "api_workers"),
    (("average", 0.35, 6), ("peak", 1.0, 15), ("burst", 3.0, 45)),
)
def test_api_profiles_have_explicit_arrival_targets_and_worker_capacity(
    profile: str,
    target_rps: float,
    api_workers: int,
) -> None:
    plan = build_run_plan(
        scenario="api",
        api_profile=profile,
        host="http://127.0.0.1",
        run_time="5m",
    )

    assert plan.target_rps == target_rps
    assert plan.api_workers == api_workers
    assert plan.users == api_workers
    assert plan.locustfile.name == "api_locust.py"


def test_burst_defaults_to_one_minute_while_steady_profiles_use_five() -> None:
    assert default_run_time("average") == "5m"
    assert default_run_time("peak") == "5m"
    assert default_run_time("burst") == "1m"


def test_ui_and_mixed_plans_preserve_25_human_sessions() -> None:
    ui = build_run_plan(
        scenario="ui",
        api_profile="peak",
        host="http://127.0.0.1",
        run_time="5m",
    )
    mixed = build_run_plan(
        scenario="mixed",
        api_profile="peak",
        host="http://127.0.0.1",
        run_time="5m",
    )

    assert ui.ui_users == 25
    assert ui.users == 25
    assert ui.locustfile.name == "ui_locust.py"
    assert mixed.ui_users == 25
    assert mixed.api_workers == 15
    assert mixed.users == 40
    assert mixed.locustfile.name == "mixed_locust.py"


def test_run_plan_rejects_remote_targets_before_locust_starts() -> None:
    with pytest.raises(ValueError):
        build_run_plan(
            scenario="api",
            api_profile="peak",
            host="https://example.com",
            run_time="5m",
        )


def test_run_plan_rejects_fractional_durations_locust_cannot_parse() -> None:
    with pytest.raises(ValueError):
        build_run_plan(
            scenario="api",
            api_profile="peak",
            host="http://127.0.0.1",
            run_time="1.5m",
        )


def test_locust_child_environment_rejects_proxy_and_distributed_overrides() -> None:
    sanitized = sanitized_locust_environment(
        {
            "PATH": "/safe/path",
            "HTTP_PROXY": "http://proxy.example",
            "https_proxy": "http://proxy.example",
            "ALL_PROXY": "socks5://proxy.example",
            "LOCUST_PROCESSES": "4",
            "LOCUST_CONFIG": "/tmp/foreign.conf",
        }
    )

    assert sanitized == {
        "PATH": "/safe/path",
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
        "LOCUST_MODE_MASTER": "false",
        "LOCUST_MODE_WORKER": "false",
        "LOCUST_PROCESSES": "0",
    }


def test_draft_save_payload_uses_get_metadata_for_cas() -> None:
    graph = {
        "nodes": [{"id": "start", "type": "startNode"}],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
        "features": {"seed_profile": "load-test"},
        "workflow_id": "workflow-id",
        "graph_hash": "a" * 64,
        "updated_at": "2026-01-01T00:00:00+00:00",
    }

    payload = build_draft_save_payload(graph)

    assert payload["expected_graph_hash"] == "a" * 64
    assert payload["expected_updated_at"] == "2026-01-01T00:00:00+00:00"
    assert payload["nodes"] == graph["nodes"]
    assert "workflow_id" not in payload
    assert "graph_hash" not in payload
    assert "updated_at" not in payload


def test_scenario_validation_errors_do_not_echo_response_bodies() -> None:
    marker = "request-marker"
    secret_canary = "response-secret-canary"
    invalid_body = {
        "status": "success",
        "run_id": "run-id",
        "detail": secret_canary,
        "results": {"answer_text": "wrong"},
    }

    error = deployment_result_error(invalid_body, marker)

    assert error == "api.result_mismatch"
    assert secret_canary not in error
    with pytest.raises(ScenarioDataError) as caught:
        build_draft_save_payload({"nodes": [secret_canary]})
    assert secret_canary not in f"{caught.value!s}\n{caught.value!r}"


def test_workflow_result_validation_requires_the_exact_terminal_answer() -> None:
    marker = "request-marker"
    expected_answer = f"Nodease load response: {marker}"

    assert execution_result_error(
        {
            "start-load-test": {"message": marker},
            "answer-load-test": {"answer_text": None},
        },
        marker,
    ) == "ui.result_mismatch"
    assert deployment_result_error(
        {
            "status": "success",
            "run_id": "run-id",
            "results": {
                "start-load-test": {"message": marker},
                "answer_text": None,
            },
        },
        marker,
    ) == "api.result_mismatch"
    assert execution_result_error(
        {"answer-load-test": {"answer_text": expected_answer}}, marker
    ) is None
    assert deployment_result_error(
        {
            "status": "success",
            "run_id": "run-id",
            "results": {"answer_text": expected_answer},
        },
        marker,
    ) is None


def test_runtime_user_pool_assigns_each_manifest_account_once() -> None:
    users = tuple(f"user-{index}" for index in range(25))
    pool = RuntimeUserPool(users)

    assigned = {pool.acquire() for _ in range(25)}

    assert assigned == set(users)
    with pytest.raises(ScenarioDataError):
        pool.acquire()


def test_api_pacing_wait_uses_request_start_time_after_initial_jitter() -> None:
    assert pacing_wait_seconds(period_seconds=15, last_start=100, now=105) == 10
    assert pacing_wait_seconds(period_seconds=15, last_start=100, now=115) == 0
    assert pacing_wait_seconds(period_seconds=15, last_start=100, now=120) == 0


def test_request_start_rate_excludes_locust_shutdown_tail() -> None:
    tracker = RequestStartRateTracker()
    tracker.reset(warmup_seconds=10, now=100)
    tracker.mark_start(now=110)
    tracker.mark_start(now=115)
    tracker.freeze(now=120)

    assert tracker.achieved_rps(now=150) == pytest.approx(0.2)


def test_stop_timeout_covers_the_longest_three_request_ui_task() -> None:
    assert STOP_TIMEOUT_SECONDS >= REQUEST_TIMEOUT_SECONDS * 3 + 10


def test_startup_tracker_keeps_login_failures_after_stats_reset() -> None:
    tracker = LoadStartupTracker()
    tracker.reset(expected_ui=25, expected_api=0)
    for index in range(24):
        tracker.mark_ui_success(f"user-{index}")
    tracker.mark_failure()

    assert tracker.is_complete()
    assert not tracker.is_valid()


def test_locustfiles_use_safe_grouped_metrics_without_response_body_logging() -> None:
    files = (
        LOAD_DIR / "locust_users.py",
        LOAD_DIR / "ui_locust.py",
        LOAD_DIR / "api_locust.py",
        LOAD_DIR / "mixed_locust.py",
    )
    combined = ""
    for path in files:
        source = path.read_text(encoding="utf-8")
        ast.parse(source)
        assert "response.text" not in source
        assert "response.content" not in source
        combined += source

    assert '"Authorization"' in combined
    assert "Bearer " in combined
    assert '"X-Organization-Id"' in combined
    assert '"/api/v1/auth/login"' in combined
    assert "STABLE_METRIC_NAMES" in combined
    assert "events.request.fire" not in combined
    assert "events.test_stopping" in combined
    assert combined.count("self.client.trust_env = False") >= 2
    assert combined.count("timeout=REQUEST_TIMEOUT_SECONDS") >= 9
    assert combined.count("allow_redirects=False") >= 9
    assert "LocalRunner" in combined
    assert "raise StopTest" in combined
    assert "STARTUP_TRACKER" in combined
