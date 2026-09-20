from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from apps.shared.tests.helpers.disposable_postgres import (
    DisposablePostgresConfig,
    DisposablePostgresConfigurationError,
    quote_disposable_database_name,
)


ROOT_DIR = Path(__file__).resolve().parents[2]
RUN_ENV = "NODEASE_RUN_DISPOSABLE_DB_TEST"
DB_PREFIX = "nodease_model_routing_activation"
pytestmark = pytest.mark.skipif(
    os.getenv(RUN_ENV) != "1",
    reason=f"set {RUN_ENV}=1 to run disposable PostgreSQL routing activation evidence",
)


def _run_alembic(database: str, config: DisposablePostgresConfig) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "apps/shared/alembic.ini",
            "upgrade",
            "heads",
        ],
        cwd=ROOT_DIR,
        env=config.subprocess_environment(database=database, root_dir=ROOT_DIR),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            "alembic failed for disposable model-routing database; "
            "stdout/stderr omitted to avoid leaking local configuration"
        )


@pytest.fixture(scope="module")
def disposable_routing_engine():
    try:
        config = DisposablePostgresConfig.from_environment()
    except DisposablePostgresConfigurationError:
        raise pytest.fail.Exception(
            "disposable PostgreSQL connection settings are not safely configured",
            pytrace=False,
        ) from None

    database = f"{DB_PREFIX}_{uuid.uuid4().hex[:12]}"
    quoted_database = quote_disposable_database_name(database, prefix=DB_PREFIX)
    admin_engine = create_engine(
        config.database_url(config.maintenance_database),
        isolation_level="AUTOCOMMIT",
    )
    database_created = False
    test_engine = None
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f"CREATE DATABASE {quoted_database}"))
        database_created = True
        extension_engine = create_engine(
            config.database_url(database),
            isolation_level="AUTOCOMMIT",
        )
        try:
            with extension_engine.connect() as connection:
                connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        finally:
            extension_engine.dispose()
        _run_alembic(database, config)
        test_engine = create_engine(config.database_url(database))
        yield test_engine
    except OperationalError:
        raise pytest.fail.Exception(
            "disposable PostgreSQL is unavailable or rejected the connection; "
            "connection details omitted",
            pytrace=False,
        ) from None
    finally:
        if test_engine is not None:
            test_engine.dispose()
        if database_created:
            try:
                with admin_engine.connect() as connection:
                    connection.execute(
                        text(
                            """
                            SELECT pg_terminate_backend(pid)
                            FROM pg_stat_activity
                            WHERE datname = :database
                              AND pid <> pg_backend_pid()
                            """
                        ),
                        {"database": database},
                    )
                    connection.execute(text(f"DROP DATABASE IF EXISTS {quoted_database}"))
            except OperationalError:
                raise pytest.fail.Exception(
                    "disposable PostgreSQL cleanup could not connect; "
                    "connection details omitted",
                    pytrace=False,
                ) from None
        admin_engine.dispose()


@pytest.fixture
def db_session(disposable_routing_engine):
    connection = disposable_routing_engine.connect()
    transaction = connection.begin()
    db = sessionmaker(
        bind=connection,
        class_=Session,
        join_transaction_mode="create_savepoint",
    )()
    try:
        yield db
    finally:
        db.close()
        transaction.rollback()
        connection.close()


def test_published_version_is_the_runtime_artifact_while_candidate_keeps_learning(
    db_session,
    monkeypatch,
):
    """발행본만 runtime에 연결되고 이후 candidate 변경은 즉시 유출되지 않는다."""

    from apps.shared.db.models.app import App
    from apps.shared.db.models.model_routing_policy import (
        LLMNodeModelRoutingLearner,
        LLMNodeModelRoutingPolicy,
    )
    from apps.shared.db.models.organization import Organization
    from apps.shared.db.models.user import User
    from apps.shared.db.models.workflow import Workflow
    from apps.shared.db.models.workflow_deployment import (
        DeploymentType,
        WorkflowDeployment,
    )
    from apps.workflow_engine.services.model_router import ModelRouter
    from apps.workflow_engine.services.model_routing_incremental_learning import (
        TASK_REQUIREMENT_FEATURE_SCHEMA_VERSION,
        IncrementalTaskRequirementClassifier,
    )
    from apps.workflow_engine.services.model_routing_judge_first_policy import (
        build_judge_first_active_policy,
    )
    from apps.workflow_engine.services.model_routing_learner_store import (
        ModelRoutingLearnerStore,
    )
    from apps.workflow_engine.services.model_routing_local_classifier import (
        MultilingualE5TaskRequirementClassifier,
    )
    from apps.workflow_engine.services.model_routing_policy_store import (
        ModelRoutingPolicyStore,
    )

    user = User(
        email=f"routing-verification-{uuid.uuid4().hex}@example.invalid",
        name="Model routing verification",
        social_provider="local",
    )
    db_session.add(user)
    db_session.flush()
    organization = Organization(
        name=f"Routing verification {uuid.uuid4().hex}",
        created_by=user.id,
        managed_by=user.id,
    )
    db_session.add(organization)
    db_session.flush()
    app = App(
        organization_id=organization.id,
        name="Routing verification",
        url_slug=f"routing-verification-{uuid.uuid4().hex}",
        created_by=user.id,
    )
    db_session.add(app)
    db_session.flush()
    workflow = Workflow(
        organization_id=organization.id,
        app_id=app.id,
        graph={},
        created_by=user.id,
    )
    db_session.add(workflow)
    db_session.flush()
    app.workflow_id = workflow.id
    deployment = WorkflowDeployment(
        app_id=app.id,
        version=1,
        type=DeploymentType.API,
        graph_snapshot={},
        created_by=user.id,
    )
    db_session.add(deployment)
    db_session.flush()

    published_artifact = {
        "kind": IncrementalTaskRequirementClassifier.ARTIFACT_KIND,
        "feature_schema_version": TASK_REQUIREMENT_FEATURE_SCHEMA_VERSION,
        "trained_example_count": 100,
        "verification_marker": "published-v1",
    }
    learner = LLMNodeModelRoutingLearner(
        organization_id=organization.id,
        workflow_id=workflow.id,
        node_id="llm-1",
        task_fingerprint="a" * 64,
        judge_contract_hash="b" * 64,
        judge_rubric_version="routing-requirements-verification",
        feature_schema_version=TASK_REQUIREMENT_FEATURE_SCHEMA_VERSION,
        encoder_model_id="verification-encoder",
        status="collecting",
        candidate_artifact=published_artifact,
        judged_request_count=100,
        selected_model_counts={"gpt-4.1-mini": 50, "gpt-5.4": 50},
        evaluation_window=[],
        recent_evaluation={
            "sample_count": 50,
            "judge_match_rate": 0.80,
            "axis_accuracies": {
                "task_complexity": 0.90,
                "decision_impact": 0.90,
                "evidence_synthesis": 0.90,
            },
            "axis_mean_errors": {
                "task_complexity": 0.20,
                "decision_impact": 0.20,
                "evidence_synthesis": 0.20,
            },
            "judge_label_diversity": 3,
            "local_prediction_diversity": 3,
            "contract_pass_rate": 1.0,
            "high_risk_underestimation_count": 0,
        },
    )
    db_session.add(learner)
    db_session.flush()
    active_policy = build_judge_first_active_policy(
        policy_version="routing-verification-v1",
        default_model_id="gpt-5.4",
        fallback_model_id="gpt-4.1-mini",
        candidate_model_ids=["gpt-4.1-mini", "gpt-5.4"],
    )
    policy = LLMNodeModelRoutingPolicy(
        organization_id=organization.id,
        workflow_id=workflow.id,
        deployment_id=deployment.id,
        node_id="llm-1",
        learner_id=learner.id,
        enabled=True,
        status="active",
        policy_version="routing-verification-v1",
        active_policy=active_policy,
        performance_checkpoint={},
        judge_user_id=user.id,
        execution_subject_user_id=user.id,
    )
    db_session.add(policy)
    db_session.flush()

    monkeypatch.setattr(
        ModelRoutingLearnerStore,
        "_outcome_rates",
        classmethod(
            lambda cls, db, *, learner_id: {
                "success_rate": 1.0,
                "schema_pass_rate": 1.0,
                "downstream_success_rate": 1.0,
                "fallback_rate": 0.0,
            }
        ),
    )
    version = ModelRoutingLearnerStore.publish_if_qualified(
        db_session,
        learner=learner,
    )
    assert version is not None
    assert policy.active_learner_version_id == version.id

    learner.candidate_artifact = {
        **published_artifact,
        "verification_marker": "candidate-v2",
        "trained_example_count": 101,
    }
    db_session.flush()
    db_session.expire_all()

    persisted_policy = ModelRoutingPolicyStore.get_runtime_policy(
        db_session,
        workflow_id=workflow.id,
        deployment_id=deployment.id,
        node_id="llm-1",
    )
    assert persisted_policy is not None
    snapshot = ModelRoutingLearnerStore.runtime_snapshot(
        db_session,
        learner_id=persisted_policy.learner_id,
        version_id=persisted_policy.active_learner_version_id,
    )
    assert snapshot is not None
    assert snapshot["mode"] == "local_first"
    assert snapshot["local_requirement_artifact"]["verification_marker"] == (
        "published-v1"
    )
    assert persisted_policy.learner_id is not None
    refreshed_learner = db_session.get(
        LLMNodeModelRoutingLearner,
        persisted_policy.learner_id,
    )
    assert refreshed_learner is not None
    assert refreshed_learner.candidate_artifact["verification_marker"] == "candidate-v2"

    def predict_published_artifact(artifact, **_kwargs):
        assert artifact["verification_marker"] == "published-v1"
        return SimpleNamespace(
            requirements={
                "task_complexity": 1,
                "decision_impact": 0,
                "evidence_synthesis": 0,
            },
            confidence=0.95,
        )

    monkeypatch.setattr(
        MultilingualE5TaskRequirementClassifier,
        "predict",
        predict_published_artifact,
    )
    monkeypatch.setattr(ModelRouter, "should_audit_local_prediction", lambda *_: False)
    decision = ModelRouter.resolve_policy(
        {
            "active_policy": persisted_policy.active_policy,
            "learner": snapshot,
        },
        inputs={"message": "간단한 사용 안내"},
        node_data=SimpleNamespace(
            model_id="gpt-5.4",
            fallback_model_id="gpt-4.1-mini",
            system_prompt="",
            user_prompt="{{ message }}",
            assistant_prompt="",
            output_format={"type": "text"},
            knowledgeBases=[],
            knowledgeCollections=[],
        ),
        available_model_ids=["gpt-4.1-mini", "gpt-5.4"],
        learning_feature_text="verification feature",
    )

    assert decision.decision_source == "local_router"
    assert decision.selected_model_id == "gpt-4.1-mini"
