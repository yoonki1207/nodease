from __future__ import annotations

import json
import stat
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from apps.shared.db.load_test_seed import (
    LOAD_TEST_REGISTERED_USER_COUNT,
    LOAD_TEST_SEED_PROFILE,
    LOAD_TEST_SEED_VERSION,
    LoadTestSeedError,
    _api_secret_state,
    build_load_test_seed_plan,
    provider_free_graph,
    redacted_seed_summary,
    verify_load_test_data,
)
from apps.shared.db.models.app import App
from apps.shared.db.models.organization import Organization
from apps.shared.db.models.organization_membership import (
    ORGANIZATION_AUTH_MANAGER,
    ORGANIZATION_MEMBERSHIP_ACTIVE,
    OrganizationMembership,
)
from apps.shared.db.models.team import UserWorkflowPermission
from apps.shared.db.models.user import User
from apps.shared.db.models.workflow import Workflow
from apps.shared.db.models.workflow_deployment import DeploymentType, WorkflowDeployment
from apps.shared.domain.app_auth_secret import (
    APP_AUTH_SECRET_VERIFIER_VERSION,
    app_auth_secret_verifier,
)
from apps.shared.schemas.auth import LoginRequest
from scripts.seed_load_test import resolve_seed_secrets, write_runtime_manifest


def test_load_seed_plan_has_25_isolated_ui_workflows_and_one_api_workflow() -> None:
    plan = build_load_test_seed_plan()

    assert LOAD_TEST_REGISTERED_USER_COUNT == 200
    assert len(plan.ui_users) == 25
    assert len(plan.background_users) == 175
    registered = (*plan.ui_users, *plan.background_users)
    assert len({spec.email for spec in registered}) == 200
    assert len({spec.user_id for spec in registered}) == 200
    assert len({spec.membership_id for spec in registered}) == 200
    assert len({spec.email for spec in plan.ui_users}) == 25
    assert len({spec.user_id for spec in plan.ui_users}) == 25
    assert len({spec.app_id for spec in plan.ui_users}) == 25
    assert len({spec.workflow_id for spec in plan.ui_users}) == 25
    assert len({spec.permission_id for spec in plan.ui_users}) == 25
    assert plan.api_workflow_id not in {spec.workflow_id for spec in plan.ui_users}
    assert plan.api_app_id not in {spec.app_id for spec in plan.ui_users}
    assert plan.api_slug == "nodease-loadtest-provider-free"


def test_every_seed_account_email_passes_the_gateway_login_schema() -> None:
    plan = build_load_test_seed_plan()

    for spec in (*plan.ui_users, *plan.background_users):
        request = LoginRequest(email=spec.email, password="synthetic-password")
        assert str(request.email) == spec.email


def test_provider_free_graph_echoes_message_without_external_node_types() -> None:
    graph = provider_free_graph()
    node_types = {node["type"] for node in graph["nodes"]}
    serialized = json.dumps(graph, sort_keys=True)

    assert node_types == {"startNode", "templateNode", "answerNode"}
    assert "{{ message }}" in serialized
    assert "credential" not in serialized.lower()
    assert "provider" not in serialized.lower()
    assert "knowledge" not in serialized.lower()
    assert "http" not in serialized.lower()


def test_seed_summary_is_versioned_and_never_contains_secrets() -> None:
    password = "load-password-secret-canary"
    token = "load-token-secret-canary"
    plan = build_load_test_seed_plan()

    summary = redacted_seed_summary(plan)
    serialized = json.dumps(summary, sort_keys=True)

    assert LOAD_TEST_SEED_PROFILE == "load-test"
    assert LOAD_TEST_SEED_VERSION == 1
    assert summary["ui_user_count"] == 25
    assert summary["registered_user_count"] == 200
    assert password not in serialized
    assert token not in serialized
    assert "password" not in serialized.lower()
    assert "token" not in serialized.lower()


def test_seed_cli_requires_environment_secrets_without_echoing_values() -> None:
    password = "load-password-secret-canary"

    try:
        resolve_seed_secrets({"LOAD_TEST_USER_PASSWORD": password})
    except RuntimeError as error:
        rendered = f"{error!s}\n{error!r}"
    else:
        raise AssertionError("missing API auth token must fail closed")

    assert password not in rendered
    assert "LOAD_TEST_AUTH_TOKEN" in rendered


def test_runtime_manifest_is_private_and_matches_the_seed_plan(tmp_path) -> None:
    password = "load-password-secret-canary"
    token = "load-token-secret-canary"
    path = tmp_path / "runtime.json"
    plan = build_load_test_seed_plan()

    write_runtime_manifest(
        path,
        plan=plan,
        user_password=password,
        api_auth_token=token,
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(payload["ui_users"]) == 25
    assert payload["ui_users"][0]["password"] == password
    assert payload["api"]["auth_token"] == token
    assert payload["api"]["workflow_id"] == str(plan.api_workflow_id)


class _SeedRows:
    def __init__(self, rows: dict[tuple[type, object], object]) -> None:
        self.rows = rows

    def get(self, model: type, row_id: object) -> object | None:
        return self.rows.get((model, row_id))


def _valid_verify_fixture():
    full_plan = build_load_test_seed_plan()
    spec = full_plan.ui_users[0]
    plan = replace(full_plan, ui_users=(spec,), background_users=())
    marker = {
        "seed_profile": LOAD_TEST_SEED_PROFILE,
        "seed_version": LOAD_TEST_SEED_VERSION,
    }
    rows: dict[tuple[type, object], object] = {
        (Organization, plan.organization_id): SimpleNamespace(
            options=marker,
            is_active=True,
        ),
        (User, spec.user_id): SimpleNamespace(
            email=spec.email,
            password=None,
            deactivated_at=None,
        ),
        (OrganizationMembership, spec.membership_id): SimpleNamespace(
            organization_id=plan.organization_id,
            user_id=spec.user_id,
            membership_state=ORGANIZATION_MEMBERSHIP_ACTIVE,
            organization_auth_state=ORGANIZATION_AUTH_MANAGER,
        ),
        (App, spec.app_id): SimpleNamespace(
            organization_id=plan.organization_id,
            url_slug=spec.app_slug,
            workflow_id=spec.workflow_id,
            active_deployment_id=None,
            is_api_enabled=False,
        ),
        (Workflow, spec.workflow_id): SimpleNamespace(
            organization_id=plan.organization_id,
            app_id=spec.app_id,
            graph=provider_free_graph(),
        ),
        (UserWorkflowPermission, spec.permission_id): SimpleNamespace(
            grantee_organization_id=plan.organization_id,
            user_id=spec.user_id,
            workflow_id=spec.workflow_id,
            auth_state="manager",
        ),
        (App, plan.api_app_id): SimpleNamespace(
            organization_id=plan.organization_id,
            url_slug=plan.api_slug,
            workflow_id=plan.api_workflow_id,
            active_deployment_id=plan.api_deployment_id,
            is_api_enabled=True,
            auth_secret=None,
        ),
        (Workflow, plan.api_workflow_id): SimpleNamespace(
            organization_id=plan.organization_id,
            app_id=plan.api_app_id,
            graph=provider_free_graph(),
        ),
        (WorkflowDeployment, plan.api_deployment_id): SimpleNamespace(
            app_id=plan.api_app_id,
            type=DeploymentType.API,
            is_active=True,
            graph_snapshot=provider_free_graph(),
        ),
        (UserWorkflowPermission, plan.api_permission_id): SimpleNamespace(
            grantee_organization_id=plan.organization_id,
            user_id=spec.user_id,
            workflow_id=plan.api_workflow_id,
            auth_state="manager",
        ),
    }
    return plan, spec, rows


def test_verify_rejects_inactive_membership_before_load_starts() -> None:
    plan, spec, rows = _valid_verify_fixture()
    rows[(OrganizationMembership, spec.membership_id)].membership_state = "suspended"

    with pytest.raises(LoadTestSeedError, match="ui_membership_invalid"):
        verify_load_test_data(_SeedRows(rows), plan=plan)


def test_verify_rejects_inactive_api_deployment_before_load_starts() -> None:
    plan, _spec, rows = _valid_verify_fixture()
    rows[(WorkflowDeployment, plan.api_deployment_id)].is_active = False

    with pytest.raises(LoadTestSeedError, match="api_deployment_invalid"):
        verify_load_test_data(_SeedRows(rows), plan=plan)


@pytest.mark.parametrize(
    ("graph_owner", "expected_error"),
    [
        ("ui_workflow", "ui_graph_invalid"),
        ("api_workflow", "api_graph_invalid"),
        ("api_deployment", "api_deployment_graph_invalid"),
    ],
)
def test_verify_rejects_tampered_provider_free_graphs_before_load_starts(
    graph_owner: str,
    expected_error: str,
) -> None:
    plan, spec, rows = _valid_verify_fixture()
    graph_rows = {
        "ui_workflow": rows[(Workflow, spec.workflow_id)].graph,
        "api_workflow": rows[(Workflow, plan.api_workflow_id)].graph,
        "api_deployment": rows[
            (WorkflowDeployment, plan.api_deployment_id)
        ].graph_snapshot,
    }
    graph_rows[graph_owner]["nodes"][1]["data"]["template"] = "tampered"

    with pytest.raises(LoadTestSeedError, match=expected_error):
        verify_load_test_data(_SeedRows(rows), plan=plan)


def test_seed_rejects_a_previous_grace_token_as_the_current_token() -> None:
    current_token = "nodease_app_current-load-token"
    previous_token = "nodease_app_previous-load-token"
    existing = SimpleNamespace(
        auth_secret_verifier=app_auth_secret_verifier(current_token),
        auth_secret_verifier_version=APP_AUTH_SECRET_VERIFIER_VERSION,
        auth_secret_generation=2,
        auth_secret_previous_verifier=app_auth_secret_verifier(previous_token),
        auth_secret_previous_verifier_version=APP_AUTH_SECRET_VERIFIER_VERSION,
        auth_secret_previous_valid_until=datetime.now(timezone.utc)
        + timedelta(minutes=5),
        auth_secret_rotated_at=datetime.now(timezone.utc),
    )

    with pytest.raises(LoadTestSeedError, match="auth_token_mismatch"):
        _api_secret_state(existing, previous_token)
