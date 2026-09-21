"""Deterministic, provider-free data for the local load-test profile."""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from apps.shared.db.models.app import App
from apps.shared.db.models.organization import Organization
from apps.shared.db.models.organization_membership import (
    ORGANIZATION_AUTH_MANAGER,
    ORGANIZATION_AUTH_MEMBER,
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
    app_auth_secret_verifier_state_is_valid,
    verify_app_auth_secret,
)
from apps.shared.services.password_hashing import hash_password, verify_password


LOAD_TEST_SEED_PROFILE = "load-test"
LOAD_TEST_SEED_VERSION = 1
LOAD_TEST_REGISTERED_USER_COUNT = 200
LOAD_TEST_UI_USER_COUNT = 25
LOAD_TEST_API_SLUG = "nodease-loadtest-provider-free"

_NAMESPACE = uuid.UUID("6b9f61f7-4ef2-4f64-8f3f-43d9ee81f317")
_ACCEPTED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
_SEED_MARKER = {
    "seed_profile": LOAD_TEST_SEED_PROFILE,
    "seed_version": LOAD_TEST_SEED_VERSION,
}


class LoadTestSeedError(RuntimeError):
    """Safe, stable error raised before a load-test seed contract is violated."""


@dataclass(frozen=True)
class LoadTestUIUserSpec:
    index: int
    email: str
    user_id: uuid.UUID
    membership_id: uuid.UUID
    app_id: uuid.UUID
    workflow_id: uuid.UUID
    permission_id: uuid.UUID
    app_slug: str


@dataclass(frozen=True)
class LoadTestBackgroundUserSpec:
    index: int
    email: str
    user_id: uuid.UUID
    membership_id: uuid.UUID


@dataclass(frozen=True)
class LoadTestSeedPlan:
    organization_id: uuid.UUID
    api_app_id: uuid.UUID
    api_workflow_id: uuid.UUID
    api_deployment_id: uuid.UUID
    api_permission_id: uuid.UUID
    api_slug: str
    ui_users: tuple[LoadTestUIUserSpec, ...]
    background_users: tuple[LoadTestBackgroundUserSpec, ...]


def _id(name: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"{LOAD_TEST_SEED_VERSION}:{name}")


def build_load_test_seed_plan() -> LoadTestSeedPlan:
    ui_users = tuple(
        LoadTestUIUserSpec(
            index=index,
            email=f"load-user-{index:02d}@load.nodease.example.com",
            user_id=_id(f"ui-user-{index:02d}"),
            membership_id=_id(f"ui-membership-{index:02d}"),
            app_id=_id(f"ui-app-{index:02d}"),
            workflow_id=_id(f"ui-workflow-{index:02d}"),
            permission_id=_id(f"ui-permission-{index:02d}"),
            app_slug=f"nodease-loadtest-ui-{index:02d}",
        )
        for index in range(1, LOAD_TEST_UI_USER_COUNT + 1)
    )
    background_users = tuple(
        LoadTestBackgroundUserSpec(
            index=index,
            email=f"load-background-{index:03d}@load.nodease.example.com",
            user_id=_id(f"background-user-{index:03d}"),
            membership_id=_id(f"background-membership-{index:03d}"),
        )
        for index in range(
            LOAD_TEST_UI_USER_COUNT + 1,
            LOAD_TEST_REGISTERED_USER_COUNT + 1,
        )
    )
    return LoadTestSeedPlan(
        organization_id=_id("organization"),
        api_app_id=_id("api-app"),
        api_workflow_id=_id("api-workflow"),
        api_deployment_id=_id("api-deployment"),
        api_permission_id=_id("api-permission"),
        api_slug=LOAD_TEST_API_SLUG,
        ui_users=ui_users,
        background_users=background_users,
    )


def _all_user_specs(
    plan: LoadTestSeedPlan,
) -> tuple[LoadTestUIUserSpec | LoadTestBackgroundUserSpec, ...]:
    return (*plan.ui_users, *plan.background_users)


def provider_free_graph() -> dict[str, Any]:
    """Return a fresh Start -> Template -> Answer graph with no external I/O."""

    graph = {
        "nodes": [
            {
                "id": "start-load-test",
                "type": "startNode",
                "position": {"x": 120, "y": 120},
                "data": {
                    "title": "Load test input",
                    "description": "Accept a deterministic test message.",
                    "displayNumber": 1,
                    "visibleProperties": [],
                    "triggerType": "manual",
                    "trigger_type": "manual",
                    "variables": [
                        {
                            "id": "message",
                            "name": "message",
                            "label": "Message",
                            "type": "paragraph",
                            "required": True,
                            "maxLength": 1000,
                            "max_length": 1000,
                        }
                    ],
                },
            },
            {
                "id": "template-load-test",
                "type": "templateNode",
                "position": {"x": 540, "y": 120},
                "data": {
                    "title": "Echo message",
                    "description": "Render the supplied value without external I/O.",
                    "displayNumber": 2,
                    "visibleProperties": [],
                    "template": "Nodease load response: {{ message }}",
                    "variables": [
                        {
                            "name": "message",
                            "value_selector": ["start-load-test", "message"],
                        }
                    ],
                },
            },
            {
                "id": "answer-load-test",
                "type": "answerNode",
                "position": {"x": 960, "y": 120},
                "data": {
                    "title": "Return result",
                    "description": "Return the deterministic result.",
                    "displayNumber": 3,
                    "visibleProperties": [],
                    "outputs": [
                        {
                            "variable": "answer_text",
                            "label": "Answer",
                            "value_selector": ["template-load-test", "text"],
                        }
                    ],
                },
            },
        ],
        "edges": [
            {
                "id": "edge-load-start-template",
                "source": "start-load-test",
                "target": "template-load-test",
            },
            {
                "id": "edge-load-template-answer",
                "source": "template-load-test",
                "target": "answer-load-test",
            },
        ],
        "viewport": {"x": 40, "y": 80, "zoom": 0.85},
    }
    return copy.deepcopy(graph)


def redacted_seed_summary(plan: LoadTestSeedPlan) -> dict[str, object]:
    return {
        "profile": LOAD_TEST_SEED_PROFILE,
        "seed_version": LOAD_TEST_SEED_VERSION,
        "organization_id": str(plan.organization_id),
        "registered_user_count": len(_all_user_specs(plan)),
        "ui_user_count": len(plan.ui_users),
        "api_slug": plan.api_slug,
    }


def prepare_load_test_data(
    db: Session,
    *,
    user_password: str,
    api_auth_token: str,
) -> LoadTestSeedPlan:
    """Upsert the dedicated load profile without rotating existing credentials.

    The caller owns the transaction and must commit or roll back. Secret mismatch
    errors deliberately use fixed messages that contain no supplied value.
    """

    _validate_secret_inputs(user_password, api_auth_token)
    plan = build_load_test_seed_plan()
    _validate_identity_conflicts(db, plan)

    for spec in _all_user_specs(plan):
        existing_user = db.get(User, spec.user_id)
        password_hash = _password_hash_for_seed_user(existing_user, user_password)
        _upsert_by_id(
            db,
            User,
            spec.user_id,
            {
                "email": spec.email,
                "name": f"Load Test User {spec.index:03d}",
                "password": password_hash,
                "social_provider": "none",
                "social_id": None,
                "avatar_url": None,
                "deactivated_at": None,
            },
        )
    db.flush()

    admin_id = plan.ui_users[0].user_id
    existing_organization = db.get(Organization, plan.organization_id)
    if existing_organization is not None and not _has_seed_marker(
        existing_organization.options
    ):
        raise LoadTestSeedError("load_test_seed.organization_marker_mismatch")
    _upsert_by_id(
        db,
        Organization,
        plan.organization_id,
        {
            "name": "Nodease Local Load Test",
            "options": dict(_SEED_MARKER),
            "flags": 0,
            "created_by": admin_id,
            "managed_by": admin_id,
            "is_active": True,
            "deactivated_at": None,
        },
    )
    db.flush()

    for spec in _all_user_specs(plan):
        _upsert_by_id(
            db,
            OrganizationMembership,
            spec.membership_id,
            {
                "organization_id": plan.organization_id,
                "user_id": spec.user_id,
                "membership_state": ORGANIZATION_MEMBERSHIP_ACTIVE,
                "organization_auth_state": (
                    ORGANIZATION_AUTH_MANAGER
                    if spec.index == 1
                    else ORGANIZATION_AUTH_MEMBER
                ),
                "invited_by": admin_id,
                "invited_at": _ACCEPTED_AT,
                "accepted_at": _ACCEPTED_AT,
                "removed_at": None,
                "options": {**_SEED_MARKER, "user_index": spec.index},
                "flags": 0,
            },
        )

    graph = provider_free_graph()
    for spec in plan.ui_users:

        existing_app = db.get(App, spec.app_id)
        _validate_existing_app(existing_app, plan.organization_id, spec.app_slug)
        _upsert_by_id(
            db,
            App,
            spec.app_id,
            {
                "organization_id": plan.organization_id,
                "name": f"Load Test UI Workflow {spec.index:02d}",
                "description": "Dedicated local load-test UI workflow.",
                "icon": {
                    "type": "emoji",
                    "content": "🧪",
                    "background_color": "#EFF6FF",
                },
                "workflow_id": (
                    existing_app.workflow_id if existing_app is not None else None
                ),
                "active_deployment_id": None,
                "url_slug": spec.app_slug,
                "auth_secret": None,
                "auth_secret_verifier": None,
                "auth_secret_verifier_version": None,
                "auth_secret_generation": 0,
                "auth_secret_previous_verifier": None,
                "auth_secret_previous_verifier_version": None,
                "auth_secret_previous_valid_until": None,
                "auth_secret_rotated_at": None,
                "is_api_enabled": False,
                "api_req_per_minute": 60,
                "api_req_per_hour": 3600,
                "is_market": False,
                "forked_from": None,
                "created_by": spec.user_id,
            },
        )
        db.flush()
        workflow = _upsert_by_id(
            db,
            Workflow,
            spec.workflow_id,
            {
                "organization_id": plan.organization_id,
                "app_id": spec.app_id,
                "graph": copy.deepcopy(graph),
                "features": {**_SEED_MARKER, "user_index": spec.index},
                "env_variables": [],
                "runtime_variables": [],
                "created_by": spec.user_id,
                "updated_by": spec.user_id,
            },
        )
        db.flush()
        app = db.get(App, spec.app_id)
        if app is None:
            raise LoadTestSeedError("load_test_seed.app_unavailable")
        app.workflow_id = workflow.id
        _upsert_by_id(
            db,
            UserWorkflowPermission,
            spec.permission_id,
            {
                "grantee_organization_id": plan.organization_id,
                "user_id": spec.user_id,
                "workflow_id": spec.workflow_id,
                "auth_state": "manager",
                "assigned_by": admin_id,
                "options": {**_SEED_MARKER, "user_index": spec.index},
                "flags": 0,
            },
        )

    _upsert_api_workflow(
        db,
        plan=plan,
        graph=graph,
        admin_id=admin_id,
        api_auth_token=api_auth_token,
    )
    db.flush()
    verify_load_test_data(
        db,
        plan=plan,
        user_password=user_password,
        api_auth_token=api_auth_token,
    )
    return plan


def verify_load_test_data(
    db: Session,
    *,
    plan: LoadTestSeedPlan | None = None,
    user_password: str | None = None,
    api_auth_token: str | None = None,
) -> dict[str, object]:
    """Verify the seed rows and return only a non-sensitive summary."""

    selected_plan = plan or build_load_test_seed_plan()
    expected_graph = provider_free_graph()
    organization = db.get(Organization, selected_plan.organization_id)
    if organization is None or not _has_seed_marker(organization.options):
        raise LoadTestSeedError("load_test_seed.organization_unavailable")
    if organization.is_active is not True:
        raise LoadTestSeedError("load_test_seed.organization_inactive")

    for spec in _all_user_specs(selected_plan):
        user = db.get(User, spec.user_id)
        membership = db.get(OrganizationMembership, spec.membership_id)
        if user is None or membership is None:
            raise LoadTestSeedError("load_test_seed.ui_binding_unavailable")
        if user.email != spec.email or user.deactivated_at is not None:
            raise LoadTestSeedError("load_test_seed.ui_user_invalid")
        expected_auth_state = (
            ORGANIZATION_AUTH_MANAGER
            if spec.index == 1
            else ORGANIZATION_AUTH_MEMBER
        )
        if (
            membership.organization_id != selected_plan.organization_id
            or membership.user_id != spec.user_id
            or membership.membership_state != ORGANIZATION_MEMBERSHIP_ACTIVE
            or membership.organization_auth_state != expected_auth_state
        ):
            raise LoadTestSeedError("load_test_seed.ui_membership_invalid")
        if user_password is not None and not verify_password(
            user_password, user.password or ""
        ):
            raise LoadTestSeedError("load_test_seed.password_mismatch")

    for spec in selected_plan.ui_users:
        app = db.get(App, spec.app_id)
        workflow = db.get(Workflow, spec.workflow_id)
        permission = db.get(UserWorkflowPermission, spec.permission_id)
        if any(row is None for row in (app, workflow, permission)):
            raise LoadTestSeedError("load_test_seed.ui_binding_unavailable")
        if (
            app.organization_id != selected_plan.organization_id
            or app.url_slug != spec.app_slug
            or app.workflow_id != spec.workflow_id
            or app.is_api_enabled is not False
            or workflow.organization_id != selected_plan.organization_id
            or workflow.app_id != spec.app_id
        ):
            raise LoadTestSeedError("load_test_seed.ui_binding_invalid")
        if workflow.graph != expected_graph:
            raise LoadTestSeedError("load_test_seed.ui_graph_invalid")
        if (
            permission.grantee_organization_id != selected_plan.organization_id
            or permission.user_id != spec.user_id
            or permission.workflow_id != spec.workflow_id
            or permission.auth_state != "manager"
        ):
            raise LoadTestSeedError("load_test_seed.ui_permission_invalid")

    api_app = db.get(App, selected_plan.api_app_id)
    api_workflow = db.get(Workflow, selected_plan.api_workflow_id)
    deployment = db.get(WorkflowDeployment, selected_plan.api_deployment_id)
    api_permission = db.get(
        UserWorkflowPermission,
        selected_plan.api_permission_id,
    )
    if any(
        row is None
        for row in (api_app, api_workflow, deployment, api_permission)
    ):
        raise LoadTestSeedError("load_test_seed.api_binding_unavailable")
    if api_app.auth_secret is not None:
        raise LoadTestSeedError("load_test_seed.raw_secret_present")
    if (
        api_app.organization_id != selected_plan.organization_id
        or api_app.url_slug != selected_plan.api_slug
        or api_app.workflow_id != selected_plan.api_workflow_id
        or api_app.is_api_enabled is not True
        or api_workflow.organization_id != selected_plan.organization_id
        or api_workflow.app_id != selected_plan.api_app_id
    ):
        raise LoadTestSeedError("load_test_seed.api_binding_invalid")
    if api_workflow.graph != expected_graph:
        raise LoadTestSeedError("load_test_seed.api_graph_invalid")
    if (
        api_app.active_deployment_id != selected_plan.api_deployment_id
        or deployment.app_id != selected_plan.api_app_id
        or deployment.type != DeploymentType.API
        or deployment.is_active is not True
    ):
        raise LoadTestSeedError("load_test_seed.api_deployment_invalid")
    if deployment.graph_snapshot != expected_graph:
        raise LoadTestSeedError("load_test_seed.api_deployment_graph_invalid")
    admin_id = selected_plan.ui_users[0].user_id
    if (
        api_permission.grantee_organization_id != selected_plan.organization_id
        or api_permission.user_id != admin_id
        or api_permission.workflow_id != selected_plan.api_workflow_id
        or api_permission.auth_state != "manager"
    ):
        raise LoadTestSeedError("load_test_seed.api_permission_invalid")
    if api_auth_token is not None and not verify_app_auth_secret(
        api_auth_token,
        current_verifier=api_app.auth_secret_verifier,
        current_verifier_version=api_app.auth_secret_verifier_version,
    ):
        raise LoadTestSeedError("load_test_seed.auth_token_mismatch")
    return redacted_seed_summary(selected_plan)


def _upsert_api_workflow(
    db: Session,
    *,
    plan: LoadTestSeedPlan,
    graph: dict[str, Any],
    admin_id: uuid.UUID,
    api_auth_token: str,
) -> None:
    existing_app = db.get(App, plan.api_app_id)
    _validate_existing_app(existing_app, plan.organization_id, plan.api_slug)
    secret_state = _api_secret_state(existing_app, api_auth_token)
    app_values = {
        "organization_id": plan.organization_id,
        "name": "Load Test API Workflow",
        "description": "Dedicated local load-test API workflow.",
        "icon": {
            "type": "emoji",
            "content": "⚙️",
            "background_color": "#ECFDF5",
        },
        "workflow_id": existing_app.workflow_id if existing_app is not None else None,
        "active_deployment_id": (
            existing_app.active_deployment_id if existing_app is not None else None
        ),
        "url_slug": plan.api_slug,
        "auth_secret": None,
        "is_api_enabled": True,
        "api_req_per_minute": 600,
        "api_req_per_hour": 36000,
        "is_market": False,
        "forked_from": None,
        "created_by": admin_id,
        **secret_state,
    }
    _upsert_by_id(db, App, plan.api_app_id, app_values)
    db.flush()
    workflow = _upsert_by_id(
        db,
        Workflow,
        plan.api_workflow_id,
        {
            "organization_id": plan.organization_id,
            "app_id": plan.api_app_id,
            "graph": copy.deepcopy(graph),
            "features": {**_SEED_MARKER, "purpose": "api"},
            "env_variables": [],
            "runtime_variables": [],
            "created_by": admin_id,
            "updated_by": admin_id,
        },
    )
    db.flush()
    deployment = _upsert_by_id(
        db,
        WorkflowDeployment,
        plan.api_deployment_id,
        {
            "app_id": plan.api_app_id,
            "version": 1,
            "type": DeploymentType.API,
            "graph_snapshot": copy.deepcopy(graph),
            "config": dict(_SEED_MARKER),
            "browser_access_policy": None,
            "input_schema": {
                "variables": [
                    {"name": "message", "type": "text", "label": "Message"}
                ]
            },
            "output_schema": {
                "outputs": [{"variable": "answer_text", "label": "Answer"}]
            },
            "description": "Provider-free local load-test deployment.",
            "created_by": admin_id,
            "is_active": True,
        },
    )
    db.flush()
    app = db.get(App, plan.api_app_id)
    if app is None:
        raise LoadTestSeedError("load_test_seed.api_app_unavailable")
    app.workflow_id = workflow.id
    app.active_deployment_id = deployment.id
    _upsert_by_id(
        db,
        UserWorkflowPermission,
        plan.api_permission_id,
        {
            "grantee_organization_id": plan.organization_id,
            "user_id": admin_id,
            "workflow_id": plan.api_workflow_id,
            "auth_state": "manager",
            "assigned_by": admin_id,
            "options": {**_SEED_MARKER, "purpose": "api"},
            "flags": 0,
        },
    )


def _validate_secret_inputs(user_password: str, api_auth_token: str) -> None:
    if not isinstance(user_password, str) or not user_password:
        raise LoadTestSeedError("load_test_seed.user_password_required")
    if not isinstance(api_auth_token, str) or not api_auth_token:
        raise LoadTestSeedError("load_test_seed.api_auth_token_required")


def _password_hash_for_seed_user(existing: User | None, password: str) -> str:
    if existing is None or existing.password is None:
        return hash_password(password)
    if not verify_password(password, existing.password):
        raise LoadTestSeedError("load_test_seed.password_mismatch")
    return existing.password


def _api_secret_state(existing: App | None, candidate: str) -> dict[str, object]:
    if existing is None:
        return {
            "auth_secret_verifier": app_auth_secret_verifier(candidate),
            "auth_secret_verifier_version": APP_AUTH_SECRET_VERIFIER_VERSION,
            "auth_secret_generation": 1,
            "auth_secret_previous_verifier": None,
            "auth_secret_previous_verifier_version": None,
            "auth_secret_previous_valid_until": None,
            "auth_secret_rotated_at": _ACCEPTED_AT,
        }
    if not app_auth_secret_verifier_state_is_valid(
        existing.auth_secret_verifier,
        existing.auth_secret_verifier_version,
    ):
        raise LoadTestSeedError("load_test_seed.auth_secret_state_invalid")
    if not verify_app_auth_secret(
        candidate,
        current_verifier=existing.auth_secret_verifier,
        current_verifier_version=existing.auth_secret_verifier_version,
    ):
        raise LoadTestSeedError("load_test_seed.auth_token_mismatch")
    return {
        "auth_secret_verifier": existing.auth_secret_verifier,
        "auth_secret_verifier_version": existing.auth_secret_verifier_version,
        "auth_secret_generation": existing.auth_secret_generation,
        "auth_secret_previous_verifier": existing.auth_secret_previous_verifier,
        "auth_secret_previous_verifier_version": (
            existing.auth_secret_previous_verifier_version
        ),
        "auth_secret_previous_valid_until": existing.auth_secret_previous_valid_until,
        "auth_secret_rotated_at": existing.auth_secret_rotated_at,
    }


def _validate_identity_conflicts(db: Session, plan: LoadTestSeedPlan) -> None:
    for spec in _all_user_specs(plan):
        existing = db.query(User).filter(User.email == spec.email).first()
        if existing is not None and existing.id != spec.user_id:
            raise LoadTestSeedError("load_test_seed.user_identity_conflict")
    for spec in plan.ui_users:
        existing_app = db.query(App).filter(App.url_slug == spec.app_slug).first()
        if existing_app is not None and existing_app.id != spec.app_id:
            raise LoadTestSeedError("load_test_seed.app_identity_conflict")
    api_app = db.query(App).filter(App.url_slug == plan.api_slug).first()
    if api_app is not None and api_app.id != plan.api_app_id:
        raise LoadTestSeedError("load_test_seed.api_identity_conflict")


def _validate_existing_app(
    app: App | None,
    organization_id: uuid.UUID,
    expected_slug: str,
) -> None:
    if app is None:
        return
    if app.organization_id != organization_id or app.url_slug != expected_slug:
        raise LoadTestSeedError("load_test_seed.app_binding_mismatch")


def _has_seed_marker(options: object) -> bool:
    return isinstance(options, dict) and all(
        options.get(key) == value for key, value in _SEED_MARKER.items()
    )


def _upsert_by_id(
    db: Session,
    model: type,
    row_id: uuid.UUID,
    values: dict[str, Any],
):
    unknown_keys = sorted(set(values) - set(sa_inspect(model).attrs.keys()))
    if unknown_keys:
        raise LoadTestSeedError("load_test_seed.unmapped_attribute")
    row = db.get(model, row_id)
    if row is None:
        row = model(id=row_id)
        db.add(row)
    for key, value in values.items():
        setattr(row, key, value)
    return row


__all__ = [
    "LOAD_TEST_API_SLUG",
    "LOAD_TEST_REGISTERED_USER_COUNT",
    "LOAD_TEST_SEED_PROFILE",
    "LOAD_TEST_SEED_VERSION",
    "LOAD_TEST_UI_USER_COUNT",
    "LoadTestSeedError",
    "LoadTestBackgroundUserSpec",
    "LoadTestSeedPlan",
    "LoadTestUIUserSpec",
    "build_load_test_seed_plan",
    "prepare_load_test_data",
    "provider_free_graph",
    "redacted_seed_summary",
    "verify_load_test_data",
]
