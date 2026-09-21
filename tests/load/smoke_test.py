"""Minimal authenticated deployment smoke test for a local Nodease instance."""

import os
from pathlib import Path
import uuid

from locust import HttpUser, between, events, task
from locust.exception import StopTest
from locust.runners import LocalRunner

from tests.load.config import (
    DEFAULT_TARGET_HOST,
    load_runtime_manifest,
    validate_target_host,
)
from tests.load.scenario_common import deployment_result_error
from tests.load.scenario_contract import REQUEST_TIMEOUT_SECONDS


LOAD_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = Path(
    os.getenv("LOAD_TEST_RUNTIME_MANIFEST", LOAD_DIR / ".env.runtime.local.json")
)


@events.test_start.add_listener
def _validate_test_target(environment, **_kwargs) -> None:
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


class SmokeWorkflowUser(HttpUser):
    """Runs a deterministic, provider-free deployment with one safe input."""

    wait_time = between(1, 2)

    def on_start(self) -> None:
        self.client.trust_env = False
        runtime = load_runtime_manifest(MANIFEST_PATH)
        self.deployment_slug = runtime.api.deployment_slug
        self.auth_token = runtime.api.auth_token

    @task
    def run_deployment(self) -> None:
        marker = f"smoke-{uuid.uuid4()}"
        headers = {
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json",
        }
        payload = {"inputs": {"message": marker}}

        with self.client.post(
            f"/api/v1/run/{self.deployment_slug}",
            json=payload,
            headers=headers,
            name="POST /api/v1/run/[smoke-deployment]",
            catch_response=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        ) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}")
                return
            try:
                body = response.json()
            except (TypeError, ValueError):
                body = None
            error = deployment_result_error(body, marker)
            if error is not None:
                response.failure(error)
                return
            response.success()
