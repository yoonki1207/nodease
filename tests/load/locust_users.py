"""Locust user implementations for local provider-free Nodease scenarios."""

from __future__ import annotations

import json
import os
import random
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import gevent
from locust import HttpUser, between, events, task
from locust.exception import StopTest, StopUser
from locust.runners import LocalRunner

from tests.load.config import (
    DEFAULT_TARGET_HOST,
    load_runtime_manifest,
    validate_target_host,
)
from tests.load.run_scenario import empty_loadgen_result, persist_loadgen_result
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
    STABLE_METRIC_NAMES,
    classify_achieved_rate,
    minimum_pacing_workers,
)


LOAD_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = Path(
    os.getenv("LOAD_TEST_RUNTIME_MANIFEST", LOAD_DIR / ".env.runtime.local.json")
)
MANIFEST = load_runtime_manifest(MANIFEST_PATH)
UI_ACCOUNT_POOL = RuntimeUserPool(MANIFEST.ui_users)


def _environment_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _environment_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


UI_EXPECTED_USERS = _environment_int("LOAD_TEST_UI_USERS", -1)
API_TARGET_RPS = _environment_float("LOAD_TEST_API_TARGET_RPS", -1.0)
API_WORKERS = _environment_int("LOAD_TEST_API_WORKERS", -1)
API_LATENCY_BUDGET_SECONDS = _environment_float(
    "LOAD_TEST_API_LATENCY_BUDGET_SECONDS", 10.0
)
LOADGEN_RESULT_PATH = os.getenv("LOAD_TEST_LOADGEN_RESULT_PATH")
_SAFE_PERIOD_SECONDS = (
    API_WORKERS / API_TARGET_RPS
    if API_TARGET_RPS > 0 and API_WORKERS > 0
    else 15.0
)


API_START_TRACKER = RequestStartRateTracker()
STARTUP_TRACKER = LoadStartupTracker()
ACTIVE_ENVIRONMENT: Any | None = None


def _safe_json(response: Any) -> dict[str, Any] | list[Any] | None:
    try:
        body = response.json()
    except (TypeError, ValueError):
        return None
    return body if isinstance(body, (dict, list)) else None


def _status_failure(response: Any, prefix: str) -> None:
    response.failure(f"{prefix}.http_{response.status_code}")


@events.test_start.add_listener
def _on_test_start(environment, **_kwargs) -> None:
    global ACTIVE_ENVIRONMENT
    ACTIVE_ENVIRONMENT = environment
    UI_ACCOUNT_POOL.reset()
    if not isinstance(environment.runner, LocalRunner):
        environment.process_exit_code = 2
        raise StopTest("load_test.single_process_required")
    actual_host = environment.host or os.getenv(
        "LOAD_TEST_TARGET_HOST", DEFAULT_TARGET_HOST
    )
    try:
        validate_target_host(actual_host)
    except ValueError:
        environment.process_exit_code = 2
        raise StopTest("load_test.target_host_invalid") from None
    if (
        UI_EXPECTED_USERS < 0
        or API_WORKERS < 0
        or API_TARGET_RPS < 0
        or (API_WORKERS == 0) != (API_TARGET_RPS == 0)
        or UI_EXPECTED_USERS + API_WORKERS <= 0
    ):
        environment.process_exit_code = 2
        raise StopTest("load_test.profile_environment_invalid")
    STARTUP_TRACKER.reset(
        expected_ui=UI_EXPECTED_USERS,
        expected_api=API_WORKERS,
    )


@events.spawning_complete.add_listener
def _on_spawning_complete(user_count: int, **_kwargs) -> None:
    deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS + 5
    while not STARTUP_TRACKER.is_complete() and time.monotonic() < deadline:
        gevent.sleep(0.05)
    expected_users = UI_EXPECTED_USERS + API_WORKERS
    if (
        ACTIVE_ENVIRONMENT is None
        or user_count != expected_users
        or not STARTUP_TRACKER.is_valid()
    ):
        if ACTIVE_ENVIRONMENT is not None:
            ACTIVE_ENVIRONMENT.process_exit_code = 2
        raise StopTest("load_test.user_startup_invalid")
    API_START_TRACKER.reset(
        warmup_seconds=(
            API_WORKERS / API_TARGET_RPS
            if API_TARGET_RPS > 0 and API_WORKERS > 0
            else 0.0
        )
    )


@events.test_stopping.add_listener
def _on_test_stopping(**_kwargs) -> None:
    API_START_TRACKER.freeze()


@events.test_stop.add_listener
def _on_test_stop(environment, **_kwargs) -> None:
    loadgen = empty_loadgen_result()
    classification = None
    if API_TARGET_RPS > 0 and API_WORKERS > 0:
        achieved = API_START_TRACKER.achieved_rps()
        classification = classify_achieved_rate(API_TARGET_RPS, achieved)
        loadgen = {
            "target_start_rate_rps": API_TARGET_RPS,
            "achieved_start_rate_rps": achieved,
            "started_total": API_START_TRACKER.started,
            "classification": classification,
        }
    print(
        json.dumps(
            {"loadgen": loadgen},
            sort_keys=True,
        )
    )
    if LOADGEN_RESULT_PATH:
        try:
            persist_loadgen_result(Path(LOADGEN_RESULT_PATH), loadgen)
        except (OSError, ValueError):
            environment.process_exit_code = 2
            print(
                "error: load-generator result artifact could not be written",
                file=sys.stderr,
            )
    if classification == "invalid":
        environment.process_exit_code = 2


class UiWorkflowUserBase(HttpUser):
    abstract = True
    wait_time = between(2, 5)

    def on_start(self) -> None:
        self.client.trust_env = False
        try:
            self.runtime_user = UI_ACCOUNT_POOL.acquire()
        except ScenarioDataError:
            STARTUP_TRACKER.mark_failure()
            self.environment.process_exit_code = 2
            raise StopUser() from None
        self.organization_headers = {
            "X-Organization-Id": str(MANIFEST.organization_id)
        }
        startup_failed = False
        with self.client.post(
            "/api/v1/auth/login",
            json={
                "email": self.runtime_user.email,
                "password": self.runtime_user.password,
            },
            name=STABLE_METRIC_NAMES["ui_auth_login"],
            catch_response=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        ) as response:
            if response.status_code != 200:
                _status_failure(response, "ui.login")
                startup_failed = True
            elif not self.client.cookies.get("auth_token"):
                response.failure("ui.login.cookie_missing")
                startup_failed = True
            else:
                response.success()
        if startup_failed:
            STARTUP_TRACKER.mark_failure()
            self.environment.process_exit_code = 2
            raise StopUser()
        STARTUP_TRACKER.mark_ui_success(self.runtime_user.email)

    @task(6)
    def read_builder_state(self) -> None:
        with self.client.get(
            "/api/v1/apps",
            headers=self.organization_headers,
            name=STABLE_METRIC_NAMES["ui_apps_list"],
            catch_response=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        ) as response:
            body = _safe_json(response)
            if response.status_code != 200:
                _status_failure(response, "ui.apps")
            elif not isinstance(body, list):
                response.failure("ui.apps.response_shape_invalid")
            else:
                response.success()

        workflow_id = str(self.runtime_user.workflow_id)
        with self.client.get(
            f"/api/v1/workflows/{workflow_id}",
            name=STABLE_METRIC_NAMES["ui_workflow_read"],
            catch_response=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        ) as response:
            body = _safe_json(response)
            if response.status_code != 200:
                _status_failure(response, "ui.workflow")
            elif not isinstance(body, dict) or body.get("id") != workflow_id:
                response.failure("ui.workflow.response_shape_invalid")
            else:
                response.success()

        with self.client.get(
            f"/api/v1/workflows/{workflow_id}/draft",
            name=STABLE_METRIC_NAMES["ui_workflow_draft_read"],
            catch_response=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        ) as response:
            body = _safe_json(response)
            if response.status_code != 200:
                _status_failure(response, "ui.draft_read")
            elif not isinstance(body, dict) or not isinstance(
                body.get("graph_hash"), str
            ):
                response.failure("ui.draft.response_shape_invalid")
            else:
                response.success()

    @task(1)
    def save_same_draft_with_cas(self) -> None:
        workflow_id = str(self.runtime_user.workflow_id)
        payload: dict[str, Any]
        with self.client.get(
            f"/api/v1/workflows/{workflow_id}/draft",
            name=STABLE_METRIC_NAMES["ui_workflow_draft_read"],
            catch_response=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        ) as response:
            body = _safe_json(response)
            if response.status_code != 200:
                _status_failure(response, "ui.draft_read")
                return
            if not isinstance(body, dict):
                response.failure("ui.draft.response_shape_invalid")
                return
            try:
                payload = build_draft_save_payload(body)
            except ScenarioDataError as error:
                response.failure(str(error))
                return
            response.success()

        with self.client.post(
            f"/api/v1/workflows/{workflow_id}/draft",
            json=payload,
            headers=self.organization_headers,
            name=STABLE_METRIC_NAMES["ui_workflow_draft_save"],
            catch_response=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        ) as response:
            body = _safe_json(response)
            if response.status_code != 200:
                _status_failure(response, "ui.draft_save")
            elif not isinstance(body, dict) or body.get("status") != "success":
                response.failure("ui.draft_save.response_shape_invalid")
            else:
                response.success()

    @task(2)
    def execute_workflow(self) -> None:
        workflow_id = str(self.runtime_user.workflow_id)
        marker = f"ui-{uuid.uuid4()}"
        with self.client.post(
            f"/api/v1/workflows/{workflow_id}/execute",
            json={"message": marker},
            headers=self.organization_headers,
            name=STABLE_METRIC_NAMES["ui_workflow_execute"],
            catch_response=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        ) as response:
            if response.status_code != 200:
                _status_failure(response, "ui.execute")
                return
            error = execution_result_error(_safe_json(response), marker)
            if error is not None:
                response.failure(error)
            else:
                response.success()

    @task(1)
    def list_recent_runs(self) -> None:
        workflow_id = str(self.runtime_user.workflow_id)
        with self.client.get(
            f"/api/v1/workflows/{workflow_id}/runs?limit=5",
            name=STABLE_METRIC_NAMES["ui_trace_lookup"],
            catch_response=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        ) as response:
            body = _safe_json(response)
            if response.status_code != 200:
                _status_failure(response, "ui.trace")
            elif not isinstance(body, dict) or not isinstance(body.get("items"), list):
                response.failure("ui.trace.response_shape_invalid")
            else:
                response.success()


class ApiWorkflowUserBase(HttpUser):
    abstract = True

    def wait_time(self) -> float:
        return pacing_wait_seconds(
            period_seconds=self.api_period_seconds,
            last_start=self.last_api_start,
            now=time.monotonic(),
        )

    def on_start(self) -> None:
        self.client.trust_env = False
        required_workers = minimum_pacing_workers(
            API_TARGET_RPS,
            API_LATENCY_BUDGET_SECONDS,
        )
        if API_WORKERS < required_workers:
            STARTUP_TRACKER.mark_failure()
            self.environment.process_exit_code = 2
            raise StopUser()
        self.api_headers = {
            "Authorization": f"Bearer {MANIFEST.api.auth_token}",
            "Content-Type": "application/json",
        }
        self.api_period_seconds = _SAFE_PERIOD_SECONDS
        self.last_api_start: float | None = None
        STARTUP_TRACKER.mark_api_success()
        gevent.sleep(random.uniform(0, self.api_period_seconds))

    @task
    def run_deployment(self) -> None:
        marker = f"api-{uuid.uuid4()}"
        self.last_api_start = time.monotonic()
        API_START_TRACKER.mark_start()
        with self.client.post(
            f"/api/v1/run/{MANIFEST.api.deployment_slug}",
            json={"inputs": {"message": marker}},
            headers=self.api_headers,
            name=STABLE_METRIC_NAMES["api_deployment_run"],
            catch_response=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        ) as response:
            if response.status_code != 200:
                _status_failure(response, "api.deployment")
                return
            error = deployment_result_error(_safe_json(response), marker)
            if error is not None:
                response.failure(error)
            else:
                response.success()


__all__ = [
    "API_WORKERS",
    "ApiWorkflowUserBase",
    "UiWorkflowUserBase",
]
