"""Isolated, persisted WorkflowEngine verification for model routing.

This module intentionally has no CLI.  The public verification command owns
authorization and passes a shared budget ledger here.  Reports contain only
case identifiers, routing metrics, and aggregate budget accounting.
"""

from __future__ import annotations

import copy
import json
import os
import random
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable

from scripts.model_routing_verification import artifact_hash, assess_holdout, _write_report
from scripts.model_routing_verification_provider import BudgetedClient

ISOLATION_ENV = "NODEASE_ROUTING_VERIFICATION_ISOLATED_DB"
NODE_ID = "llm-triage"
LOW_MODEL = "gpt-4o-mini"
MID_MODEL = "gpt-5.4-mini"
HIGH_MODEL = "gpt-5.6-sol"
FIXED_CANDIDATE_MODELS = (LOW_MODEL, MID_MODEL, HIGH_MODEL)


class IsolationRequiredError(RuntimeError):
    pass


class RuntimeVerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class _ResourceIds:
    namespace: uuid.UUID
    app: uuid.UUID
    workflow: uuid.UUID
    automatic: uuid.UUID
    mid: uuid.UUID
    high: uuid.UUID
    policy: uuid.UUID

    @classmethod
    def fresh(cls) -> "_ResourceIds":
        namespace = uuid.uuid4()
        make = lambda name: uuid.uuid5(namespace, name)
        return cls(
            namespace, make("app"), make("workflow"), make("automatic"),
            make("mid"), make("high"), make("policy"),
        )

    def run(self, arm: str, case_id: str) -> uuid.UUID:
        return uuid.uuid5(self.namespace, f"run:{arm}:{case_id}")


@contextmanager
def budget_all_runtime_clients(
    ledger,
    allowed_models: Iterable[str] | None = None,
    *,
    visible_models: Iterable[str] | None = None,
):
    """Wrap the shared runtime resolver and lock its visible candidate set."""

    from apps.workflow_engine.services.llm_service import LLMService

    allowed = tuple(dict.fromkeys(str(item) for item in (allowed_models or ())))
    visible = tuple(dict.fromkeys(str(item) for item in (visible_models or allowed)))
    original_client = LLMService.get_runtime_client_for_user
    original_available = LLMService.get_runtime_available_model_ids_for_user

    def bounded(db, user_id, model_id, organization_id=None):
        if allowed and model_id not in allowed:
            raise RuntimeVerificationError("model_outside_preregistered_candidates")
        selection = original_client(db, user_id, model_id, organization_id)
        if isinstance(selection.client, BudgetedClient):
            return selection
        return replace(
            selection,
            client=BudgetedClient(selection.client, str(selection.model_id), ledger),
        )

    def available(db, user_id, organization_id=None):
        actual = original_available(
            db,
            user_id=user_id,
            organization_id=organization_id,
        )
        if not visible:
            return actual
        actual_set = set(actual)
        missing = set(visible) - actual_set
        if missing:
            raise RuntimeVerificationError("preregistered_model_unavailable")
        return list(visible)

    LLMService.get_runtime_client_for_user = staticmethod(bounded)
    LLMService.get_runtime_available_model_ids_for_user = staticmethod(available)
    try:
        yield
    finally:
        LLMService.get_runtime_client_for_user = original_client
        LLMService.get_runtime_available_model_ids_for_user = original_available


@contextmanager
def _synchronous_log_tasks():
    """Persist engine logs synchronously and suppress background refresh jobs."""

    from apps.log_system import tasks as log_tasks
    from apps.shared.celery_app import celery_app

    original = celery_app.send_task
    task_map = {
        "log.create_run": log_tasks.create_run_log,
        "log.update_run_finish": log_tasks.update_run_log_finish,
        "log.update_run_error": log_tasks.update_run_log_error,
        "log.create_node": log_tasks.create_node_log,
        "log.update_node_finish": log_tasks.update_node_log_finish,
        "log.update_node_error": log_tasks.update_node_log_error,
    }

    def send_task(name, args=None, kwargs=None, **_options):
        if name in task_map:
            value = task_map[name].run(*(args or []), **(kwargs or {}))
            return type("Result", (), {"get": lambda self, timeout=None: value})()
        if name.startswith("workflow.model_routing."):
            return type("Result", (), {"get": lambda self, timeout=None: {"status": "suppressed"}})()
        raise RuntimeVerificationError("unexpected_background_task")

    celery_app.send_task = send_task
    try:
        yield
    finally:
        celery_app.send_task = original


def _graph(*, model_id: str, automatic: bool, fallback: str | None = None) -> dict[str, Any]:
    from apps.shared.db.demo_seed import _ticket_ops_graph

    graph = copy.deepcopy(_ticket_ops_graph())
    start = next(node for node in graph["nodes"] if node["id"] == "webhook-ticket")
    start["data"]["variable_mappings"] = [
        {"json_path": key, "variable_name": key}
        for key in ("request", "context", "customerTier")
    ]
    node = next(node for node in graph["nodes"] if node["id"] == NODE_ID)
    node["data"].update(
        {
            "provider": "openai",
            "model_id": model_id,
            "fallback_model_id": fallback,
            "auto_model_routing": automatic,
            "model_routing_policy": {"refresh": {"refresh_every_runs": 1000}},
            "system_prompt": (
                "Use only supplied facts. Return one JSON object with fields "
                "긴급도 (boolean) and 답변 초안 (string). Include facts needed "
                "to answer the request and do not claim unsupported actions."
            ),
            "user_prompt": "Request: {{ request }}\nContext: {{ context }}",
            "referenced_variables": [
                {"name": key, "value_selector": ["webhook-ticket", key]}
                for key in ("request", "context")
            ],
            "parameters": {
                "temperature": 0,
                "max_tokens": 900,
                "response_format": {"type": "json_object"},
            },
        }
    )
    return graph


def _gold_model_contract(
    cases,
    *,
    candidate_models: Iterable[str],
    judge_model: str,
) -> tuple[dict[str, frozenset[str]], int]:
    """Map gold requirements to the catalog model the runtime should select.

    Gold requirements make this check independent of the learned classifier.
    It deliberately reuses the production catalog selector, so it verifies the
    classifier-to-model contract but is not an independent test of that selector.
    """

    from apps.shared.services.llm_model_pricing import get_model_pricing
    from apps.shared.services.model_routing_global_profile_catalog import (
        catalog_metadata_for_model_id,
    )
    from apps.workflow_engine.services.model_router import ModelRouter
    from apps.workflow_engine.services.model_routing_bootstrap_score import (
        model_bootstrap_score,
    )

    candidates = tuple(dict.fromkeys(str(model).strip() for model in candidate_models))
    if not candidates or any(
        not catalog_metadata_for_model_id(model)
        or model_bootstrap_score(model) is None
        or get_model_pricing(model) is None
        for model in candidates
    ):
        raise RuntimeVerificationError("model_selection_contract_unverifiable")

    graph = _graph(model_id=judge_model, automatic=True, fallback=HIGH_MODEL)
    try:
        node_data = next(
            node["data"] for node in graph["nodes"] if node["id"] == NODE_ID
        )
    except (KeyError, StopIteration, TypeError):
        raise RuntimeVerificationError("model_selection_contract_unverifiable") from None

    allowed: dict[str, frozenset[str]] = {}
    selected_models: set[str] = set()
    for case in cases:
        inputs = {
            "webhook-ticket": {
                "request": case.request,
                "context": case.context,
                "customerTier": "synthetic",
            }
        }
        structural_facts = ModelRouter.runtime_requirement_facts(
            inputs=inputs,
            node_data=node_data,
        )
        selected = ModelRouter.select_candidate_for_requirements(
            candidate_model_ids=candidates,
            requirements=case.requirements,
            default_model_id=judge_model,
            structural_facts=structural_facts,
        )
        if selected not in candidates:
            raise RuntimeVerificationError("model_selection_contract_unverifiable")
        allowed[case.case_id] = frozenset({selected})
        selected_models.add(selected)
    if not allowed or not selected_models:
        raise RuntimeVerificationError("model_selection_contract_unverifiable")
    return allowed, len(selected_models)


def _create_resources(ids, *, user_id, organization_id, candidates, judge_model):
    from apps.shared.db.models.app import App
    from apps.shared.db.models.model_routing_policy import LLMNodeModelRoutingPolicy
    from apps.shared.db.models.organization import Organization
    from apps.shared.db.models.user import User
    from apps.shared.db.models.workflow import Workflow
    from apps.shared.db.models.workflow_deployment import DeploymentType, WorkflowDeployment
    from apps.shared.db.session import SessionLocal
    from apps.workflow_engine.services.model_routing_bootstrap import downstream_contract_from_graph
    from apps.workflow_engine.services.model_routing_judge_first_policy import (
        build_judge_first_active_policy,
    )
    from apps.workflow_engine.services.model_routing_learner_store import ModelRoutingLearnerStore

    auto_graph = _graph(model_id=judge_model, automatic=True, fallback=HIGH_MODEL)
    db = SessionLocal()
    try:
        if db.get(User, user_id) is None or db.get(Organization, organization_id) is None:
            raise RuntimeVerificationError("execution_subject_not_found")
        checks = ((App, ids.app), (Workflow, ids.workflow))
        checks += tuple((WorkflowDeployment, item) for item in (ids.automatic, ids.mid, ids.high))
        checks += ((LLMNodeModelRoutingPolicy, ids.policy),)
        if any(db.get(model, identity) is not None for model, identity in checks):
            raise RuntimeVerificationError("fresh_resource_collision")
        app = App(
            id=ids.app, organization_id=organization_id,
            name="Routing verification", url_slug=f"routing-verify-{ids.namespace.hex}",
            created_by=user_id, is_api_enabled=False,
        )
        db.add(app)
        db.flush()
        workflow = Workflow(
            id=ids.workflow, organization_id=organization_id, app_id=ids.app,
            graph=auto_graph, features={}, env_variables=[], runtime_variables=[],
            created_by=user_id, updated_by=user_id,
        )
        db.add(workflow)
        db.flush()
        app.workflow_id = ids.workflow
        for identity, arm, graph in (
            (ids.automatic, "automatic", auto_graph),
            (ids.mid, "mid", _graph(model_id=MID_MODEL, automatic=False)),
            (ids.high, "high", _graph(model_id=HIGH_MODEL, automatic=False)),
        ):
            db.add(WorkflowDeployment(
                id=identity, app_id=ids.app, version=1, type=DeploymentType.WEBHOOK,
                graph_snapshot=graph, config={"verification": True, "arm": arm},
                input_schema={"type": "object"}, output_schema={"type": "object"},
                created_by=user_id, is_active=True,
            ))
        db.flush()
        app.active_deployment_id = ids.automatic
        node_data = next(n["data"] for n in auto_graph["nodes"] if n["id"] == NODE_ID)
        learner = ModelRoutingLearnerStore.get_or_create(
            db, organization_id=organization_id, workflow_id=ids.workflow,
            node_id=NODE_ID, node_data=node_data,
            downstream_contract=downstream_contract_from_graph(auto_graph, NODE_ID),
        )
        db.add(LLMNodeModelRoutingPolicy(
            id=ids.policy, organization_id=organization_id, workflow_id=ids.workflow,
            deployment_id=ids.automatic, node_id=NODE_ID, enabled=True, status="active",
            policy_version="persisted-verification-v1", learner_id=learner.id,
            judge_user_id=user_id, execution_subject_user_id=user_id,
            refresh_every_runs=1000,
            active_policy=build_judge_first_active_policy(
                policy_version="persisted-verification-v1",
                default_model_id=judge_model,
                fallback_model_id=HIGH_MODEL,
                candidate_model_ids=candidates,
                judge_model_id=judge_model,
            ),
        ))
        db.commit()
        return learner.id
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _run_workflow(case, *, arm, graph, ids, user_id, organization_id, preview=False):
    from apps.shared.db.models.workflow_run import WorkflowNodeRun
    from apps.shared.db.session import SessionLocal
    from apps.workflow_engine.workflow.core.workflow_engine import WorkflowEngine

    deployment = {"automatic": ids.automatic, "mid": ids.mid, "high": ids.high}[arm]
    run_id = ids.run(arm, case.case_id)
    context = {
        "workflow_id": str(ids.workflow), "workflow_run_id": str(run_id),
        "app_id": str(ids.app), "deployment_id": str(deployment),
        "workflow_version": 1, "user_id": str(user_id),
        "organization_id": str(organization_id), "trigger_mode": "webhook",
        "execution_subject": {"subject_type": "user", "subject_id": str(user_id)},
    }
    if preview:
        context.update({
            "routing_policy_preview": True,
            "routing_policy_preview_node_ids": [NODE_ID],
            "routing_policy_deployment_id": str(ids.automatic),
            "routing_policy_deployment_node_ids": [NODE_ID],
            "routing_policy_execute_judge": True,
        })
    engine = WorkflowEngine(
        graph=graph,
        user_input={"request": case.request, "context": case.context, "customerTier": "synthetic"},
        execution_context=context, is_deployed=True, workflow_timeout=120,
    )
    try:
        engine.execute()
    finally:
        engine.cleanup()
    db = SessionLocal()
    try:
        node_run = db.query(WorkflowNodeRun).filter(
            WorkflowNodeRun.workflow_run_id == run_id,
            WorkflowNodeRun.node_id == NODE_ID,
        ).first()
        if node_run is None or not str(node_run.status).lower().endswith("success"):
            raise RuntimeVerificationError("workflow_run_not_successful")
        outputs = node_run.outputs if isinstance(node_run.outputs, dict) else {}
        metadata = outputs.get("metadata") if isinstance(outputs.get("metadata"), dict) else {}
        trace = node_run.trace_metadata if isinstance(node_run.trace_metadata, dict) else {}
        llm_trace = trace.get("llm") if isinstance(trace.get("llm"), dict) else {}
        routing = (
            llm_trace.get("model_routing")
            if isinstance(llm_trace.get("model_routing"), dict)
            else metadata.get("model_routing")
            if isinstance(metadata.get("model_routing"), dict)
            else llm_trace
            if "decision_source" in llm_trace
            else {}
        )
        return {"text": str(outputs.get("text") or ""), "routing": routing, "run_id": run_id}
    finally:
        db.close()


def _record_training_run(run_id):
    from apps.shared.db.session import SessionLocal
    from apps.workflow_engine.services.model_routing_policy_store import ModelRoutingPolicyStore

    db = SessionLocal()
    try:
        ModelRoutingPolicyStore.record_completed_deployed_run(db, workflow_run_id=run_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _train_all_pending(learner_id):
    from apps.shared.db.session import SessionLocal
    from apps.workflow_engine.services.model_routing_learning_batch import ModelRoutingLearningBatchService

    db = SessionLocal()
    try:
        while True:
            result = ModelRoutingLearningBatchService.train_pending(
                db, learner_id=str(learner_id), force=True,
            )
            db.commit()
            if result.remaining_count <= 0 or result.processed_count <= 0:
                return
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _snapshot(ids, learner_id) -> dict[str, Any]:
    from apps.shared.db.models.model_routing_policy import LLMNodeModelRoutingPolicy
    from apps.shared.db.session import SessionLocal
    from apps.workflow_engine.services.model_routing_learner_store import ModelRoutingLearnerStore

    db = SessionLocal()
    try:
        policy = db.get(LLMNodeModelRoutingPolicy, ids.policy)
        snapshot = ModelRoutingLearnerStore.runtime_snapshot(
            db, learner_id=learner_id,
            version_id=policy.active_learner_version_id if policy else None,
        )
        summary = ModelRoutingLearnerStore.label_summary(db, learner_id=learner_id)
        artifact = (snapshot or {}).pop("local_requirement_artifact", None)
        return {
            "learner_id": str(learner_id),
            "policy_enabled": bool(policy and policy.enabled),
            "active_version_id": (
                str(policy.active_learner_version_id)
                if policy and policy.active_learner_version_id
                else None
            ),
            "active_version": (snapshot or {}).get("active_version"),
            "mode": (snapshot or {}).get("mode"),
            "judged_request_count": (snapshot or {}).get("judged_request_count", 0),
            "artifact_hash": artifact_hash(artifact) if artifact else None,
            **summary,
        }
    finally:
        db.close()


def _quality_content(response: Any) -> dict[str, Any]:
    try:
        value = response["choices"][0]["message"]["content"]
        parsed = json.loads(value)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        raise RuntimeVerificationError("quality_response_invalid") from None
    if not isinstance(parsed, dict):
        raise RuntimeVerificationError("quality_response_invalid")
    return parsed


def _evaluate_quality(case, outputs, *, seed, db, user_id, organization_id, quality_model):
    from apps.workflow_engine.services.llm_service import LLMService

    arms = ["automatic", "mid", "high"]
    random.Random(f"{seed}:{case.case_id}").shuffle(arms)
    aliases = {f"output_{index + 1}": arm for index, arm in enumerate(arms)}
    prompt = {
        "request": case.request, "context": case.context,
        "required_facts": list(case.required_facts),
        "forbidden_errors": list(case.forbidden_errors),
        "outputs": {alias: outputs[arm] for alias, arm in aliases.items()},
        "response_schema": {alias: {"score": "integer 0..100", "pass": "boolean"} for alias in aliases},
    }
    selection = LLMService.get_runtime_client_for_user(
        db, user_id, quality_model, organization_id,
    )
    judged = _quality_content(selection.client.invoke_sync(
        messages=[
            {"role": "system", "content": "Judge only factual task quality. Return one JSON object."},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        temperature=0, max_tokens=500, response_format={"type": "json_object"},
    ))
    result = {}
    for alias, arm in aliases.items():
        item = judged.get(alias)
        if not isinstance(item, dict) or not isinstance(item.get("pass"), bool):
            raise RuntimeVerificationError("quality_response_invalid")
        score = item.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 100:
            raise RuntimeVerificationError("quality_response_invalid")
        result[arm] = {"score": float(score), "pass": item["pass"]}
    return result


def _execute_seed(*, cases, seed, ledger, user_id, organization_id, judge_model,
                  quality_model, candidate_models, quality_callback: Callable[..., dict]):
    from apps.shared.db.session import SessionLocal

    training = [case for case in cases if case.split == "train"]
    holdout = [case for case in cases if case.split == "holdout"]
    allowed_models_by_case, expected_model_diversity = _gold_model_contract(
        holdout,
        candidate_models=candidate_models,
        judge_model=judge_model,
    )
    ids = _ResourceIds.fresh()
    learner_id = _create_resources(
        ids, user_id=user_id, organization_id=organization_id,
        candidates=candidate_models, judge_model=judge_model,
    )
    auto = _graph(model_id=judge_model, automatic=True, fallback=HIGH_MODEL)
    ordered = list(training)
    random.Random(seed).shuffle(ordered)
    for case in ordered:
        result = _run_workflow(
            case, arm="automatic", graph=auto, ids=ids,
            user_id=user_id, organization_id=organization_id,
        )
        _record_training_run(result["run_id"])
    _train_all_pending(learner_id)
    before = _snapshot(ids, learner_id)
    ready = (
        before["policy_enabled"]
        and before["mode"] == "local_first"
        and before["active_version"] is not None
        and before["active_version_id"] is not None
    )
    if not ready:
        return {
            "seed": seed, "namespace": str(ids.namespace),
            "expected_model_diversity": expected_model_diversity,
            "readiness": {"ready": False, "reason": "local_first_not_published", "state": before},
            "rows": [], "verdict": "FAIL", "failures": ["learner_not_ready"], "incomplete": [],
        }

    mid = _graph(model_id=MID_MODEL, automatic=False)
    high = _graph(model_id=HIGH_MODEL, automatic=False)
    rows = []
    for case in holdout:
        results = {
            "automatic": _run_workflow(case, arm="automatic", graph=auto, ids=ids, user_id=user_id, organization_id=organization_id, preview=True),
            "mid": _run_workflow(case, arm="mid", graph=mid, ids=ids, user_id=user_id, organization_id=organization_id),
            "high": _run_workflow(case, arm="high", graph=high, ids=ids, user_id=user_id, organization_id=organization_id),
        }
        db = SessionLocal()
        try:
            quality = quality_callback(
                case, {key: value["text"] for key, value in results.items()},
                seed=seed, db=db, user_id=user_id,
                organization_id=organization_id, quality_model=quality_model,
            )
        finally:
            db.close()
        routing = results["automatic"]["routing"]
        factors = routing.get("decision_factors") if isinstance(routing.get("decision_factors"), dict) else {}
        judge = routing.get("judge") if isinstance(routing.get("judge"), dict) else {}
        predicted = factors.get("task_requirements") or factors.get("local_prediction") or judge.get("task_requirements")
        rows.append({
            "case_id": case.case_id, "difficulty": case.difficulty,
            "gold": dict(case.requirements), "predicted": predicted,
            "confidence": factors.get("local_confidence", judge.get("confidence", 0.0)),
            "decision_source": routing.get("decision_source"),
            "model_id": routing.get("selected_model"),
            "quality_pass": quality["automatic"]["pass"],
            "quality_score": quality["automatic"]["score"],
            "mid_score": quality["mid"]["score"], "high_score": quality["high"]["score"],
        })
    after = _snapshot(ids, learner_id)
    assessment = assess_holdout(
        rows,
        expected_cases={
            case.case_id: {
                "difficulty": case.difficulty,
                "gold": dict(case.requirements),
                "allowed_model_ids": allowed_models_by_case[case.case_id],
            }
            for case in holdout
        },
        before=before,
        after=after,
        persisted_activation=(
            before["policy_enabled"]
            and before["active_version"] is not None
            and before["active_version_id"] is not None
        ),
    )
    return {
        "seed": seed, "namespace": str(ids.namespace),
        "expected_model_diversity": expected_model_diversity,
        "readiness": {"ready": True, "state": before}, "frozen_after": after,
        "rows": rows, **assessment,
    }


def run_persisted_verification(
    cases, *, output_dir: Path, ledger, user_id: uuid.UUID,
    organization_id: uuid.UUID, judge_model: str = "gpt-5.4-mini",
    quality_model: str = "gpt-5.4", candidate_models: list[str],
    seeds=(17, 42, 73),
) -> dict[str, Any]:
    """Run persisted learning and a frozen workflow holdout in an isolated DB."""

    if os.environ.get(ISOLATION_ENV) != "1":
        raise IsolationRequiredError("isolated_db_required")
    cases = list(cases)
    train = [case for case in cases if case.split == "train"]
    holdout = [case for case in cases if case.split == "holdout"]
    if len(train) != 300 or len(holdout) != 150 or len({c.case_id for c in cases}) != 450:
        raise ValueError("verification_cases_invalid")
    models = [str(item).strip() for item in candidate_models]
    if (
        len(models) != len(set(models))
        or set(models) != set(FIXED_CANDIDATE_MODELS)
        or judge_model != MID_MODEL
    ):
        raise ValueError("candidate_models_invalid")
    seeds = tuple(seeds)
    if not seeds or len(seeds) != len(set(seeds)):
        raise ValueError("seeds_invalid")
    output_dir = Path(output_dir)
    report_path = output_dir / "persisted-verification.json"
    if report_path.exists():
        raise RuntimeVerificationError("report_already_exists")
    report = {
        "scope": "isolated_persisted_workflow_verification",
        "training_count": len(train), "holdout_count": len(holdout),
        "judge_model": judge_model, "quality_model": quality_model,
        "candidate_models": models, "seeds": list(seeds), "runs": [],
        "verdict": "INCOMPLETE",
    }
    _write_report(report_path, report)
    try:
        with budget_all_runtime_clients(
            ledger,
            [*models, quality_model],
            visible_models=models,
        ), _synchronous_log_tasks():
            for seed in seeds:
                run = _execute_seed(
                    cases=cases, seed=seed, ledger=ledger, user_id=user_id,
                    organization_id=organization_id, judge_model=judge_model,
                    quality_model=quality_model, candidate_models=models,
                    quality_callback=_evaluate_quality,
                )
                report["runs"].append(run)
                report["budget"] = ledger.summary()
                _write_report(report_path, report)
    except Exception as exc:
        report["blocked_reason"] = str(getattr(exc, "reason_code", None) or type(exc).__name__)
        report["budget"] = ledger.summary()
        _write_report(report_path, report)
        return report
    verdicts = [run.get("verdict") for run in report["runs"]]
    report["verdict"] = "PASS" if verdicts and all(v == "PASS" for v in verdicts) else "FAIL" if "FAIL" in verdicts else "INCOMPLETE"
    report["budget"] = ledger.summary()
    _write_report(report_path, report)
    return report


__all__ = [
    "IsolationRequiredError", "RuntimeVerificationError",
    "budget_all_runtime_clients", "run_persisted_verification",
]
