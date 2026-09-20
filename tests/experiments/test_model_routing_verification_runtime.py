from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _cases():
    rows = []
    for split, count in (("train", 300), ("holdout", 150)):
        for index in range(count):
            rows.append(
                SimpleNamespace(
                    case_id=f"{split}-{index}",
                    split=split,
                    difficulty=("low", "medium", "high")[index % 3],
                    request=f"request {index}",
                    context=f"context {index}",
                    requirements={
                        "task_complexity": index % 4,
                        "decision_impact": index % 4,
                        "evidence_synthesis": index % 4,
                    },
                    required_facts=("fact",),
                    forbidden_errors=("error",),
                )
            )
    return rows


def test_refuses_before_touching_runtime_without_isolated_db_flag(monkeypatch, tmp_path):
    import scripts.model_routing_verification_runtime as runtime

    monkeypatch.delenv("NODEASE_ROUTING_VERIFICATION_ISOLATED_DB", raising=False)
    monkeypatch.setattr(
        runtime,
        "_execute_seed",
        lambda **_kwargs: pytest.fail("runtime must not be touched"),
    )

    with pytest.raises(runtime.IsolationRequiredError, match="isolated_db_required"):
        runtime.run_persisted_verification(
            _cases(),
            output_dir=tmp_path,
            ledger=SimpleNamespace(summary=lambda: {}),
            user_id=uuid4(),
            organization_id=uuid4(),
            candidate_models=["gpt-5.4-mini", "gpt-5.6-sol"],
        )


def test_runtime_client_facade_wraps_and_restores_every_selection(monkeypatch):
    import scripts.model_routing_verification_runtime as runtime
    from apps.workflow_engine.services.llm_service import LLMRuntimeSelection, LLMService
    from scripts.model_routing_verification_provider import BudgetLedger, BudgetedClient

    class RawClient:
        def invoke_sync(self, messages, **kwargs):
            return {
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

    original = lambda db, user_id, model_id, organization_id=None: LLMRuntimeSelection(
        client=RawClient(),
        credential_id=uuid4(),
        model_id=model_id,
        organization_id=organization_id,
    )
    monkeypatch.setattr(LLMService, "get_runtime_client_for_user", original)
    original_available = lambda db, *, user_id, organization_id: [
        "gpt-5.4-mini",
        "gpt-5.6-sol",
        "gpt-5.4",
    ]
    monkeypatch.setattr(
        LLMService, "get_runtime_available_model_ids_for_user", original_available
    )
    ledger = BudgetLedger(Decimal("1"))

    with runtime.budget_all_runtime_clients(
        ledger,
        ["gpt-5.4-mini", "gpt-5.6-sol", "gpt-5.4"],
        visible_models=["gpt-5.4-mini", "gpt-5.6-sol"],
    ):
        assert LLMService.get_runtime_available_model_ids_for_user(
            object(), user_id=uuid4(), organization_id=uuid4()
        ) == ["gpt-5.4-mini", "gpt-5.6-sol"]
        selection = LLMService.get_runtime_client_for_user(
            object(), uuid4(), "gpt-5.4-mini", uuid4()
        )
        assert isinstance(selection.client, BudgetedClient)
        selection.client.invoke_sync(
            [{"role": "user", "content": "bounded"}], max_tokens=1
        )

    assert LLMService.get_runtime_client_for_user is original
    assert LLMService.get_runtime_available_model_ids_for_user is original_available
    assert ledger.summary()["calls"]["attempted"] == 1


def test_readiness_failure_is_fail_closed_and_skips_quality(monkeypatch, tmp_path):
    import scripts.model_routing_verification_runtime as runtime

    monkeypatch.setenv("NODEASE_ROUTING_VERIFICATION_ISOLATED_DB", "1")
    quality_calls = []

    def fake_seed(**kwargs):
        assert kwargs["quality_callback"] is runtime._evaluate_quality
        quality_calls.append("seed-entered")
        return {
            "seed": kwargs["seed"],
            "readiness": {"ready": False, "reason": "local_first_not_published"},
            "rows": [],
            "verdict": "FAIL",
            "failures": ["learner_not_ready"],
            "incomplete": [],
        }

    monkeypatch.setattr(runtime, "_execute_seed", fake_seed)
    report = runtime.run_persisted_verification(
        _cases(),
        output_dir=tmp_path,
        ledger=SimpleNamespace(summary=lambda: {"calls": {"attempted": 0}}),
        user_id=uuid4(),
        organization_id=uuid4(),
        candidate_models=["gpt-4o-mini", "gpt-5.4-mini", "gpt-5.6-sol"],
        seeds=(17,),
    )

    assert quality_calls == ["seed-entered"]
    assert report["verdict"] == "FAIL"
    assert report["runs"][0]["failures"] == ["learner_not_ready"]
    assert not any("output" in key for key in report["runs"][0])


def test_seed_readiness_failure_does_not_enter_holdout_or_quality(monkeypatch):
    import scripts.model_routing_verification_runtime as runtime

    cases = _cases()
    training_ids = {case.case_id for case in cases if case.split == "train"}
    executed = []
    monkeypatch.setattr(runtime, "_create_resources", lambda *args, **kwargs: uuid4())
    monkeypatch.setattr(
        runtime,
        "_gold_model_contract",
        lambda holdout, **_kwargs: (
            {case.case_id: frozenset({"gpt-5.4-mini"}) for case in holdout},
            1,
        ),
    )
    monkeypatch.setattr(runtime, "_graph", lambda **kwargs: {})

    def fake_run(case, **kwargs):
        executed.append(case.case_id)
        return {"run_id": uuid4()}

    monkeypatch.setattr(runtime, "_run_workflow", fake_run)
    recorded = []
    monkeypatch.setattr(
        runtime, "_record_training_run", lambda run_id: recorded.append(run_id)
    )

    def train_after_all_records(_learner_id):
        assert len(recorded) == 300

    monkeypatch.setattr(runtime, "_train_all_pending", train_after_all_records)
    monkeypatch.setattr(
        runtime,
        "_snapshot",
        lambda *args, **kwargs: {
            "policy_enabled": True,
            "mode": "judge_first",
            "active_version": None,
            "active_version_id": None,
            "judged_request_count": 300,
        },
    )

    result = runtime._execute_seed(
        cases=cases,
        seed=17,
        ledger=object(),
        user_id=uuid4(),
        organization_id=uuid4(),
        judge_model="gpt-5.4-mini",
        quality_model="gpt-5.4",
        candidate_models=["gpt-4o-mini", "gpt-5.4-mini", "gpt-5.6-sol"],
        quality_callback=lambda *args, **kwargs: pytest.fail("quality must be skipped"),
    )

    assert set(executed) == training_ids
    assert len(executed) == 300
    assert result["verdict"] == "FAIL"
    assert result["rows"] == []


def test_seed_assessment_receives_preregistered_holdout_cases(monkeypatch):
    import scripts.model_routing_verification_runtime as runtime

    cases = _cases()
    holdout = [case for case in cases if case.split == "holdout"]
    expected_allowed, expected_diversity = runtime._gold_model_contract(
        holdout,
        candidate_models=["gpt-4o-mini", "gpt-5.4-mini", "gpt-5.6-sol"],
        judge_model="gpt-5.4-mini",
    )
    learner_id = uuid4()
    monkeypatch.setattr(runtime, "_create_resources", lambda *args, **kwargs: learner_id)
    monkeypatch.setattr(
        runtime,
        "_gold_model_contract",
        lambda _holdout, **_kwargs: (expected_allowed, expected_diversity),
    )
    monkeypatch.setattr(runtime, "_graph", lambda **kwargs: {})
    monkeypatch.setattr(runtime, "_record_training_run", lambda _run_id: None)
    monkeypatch.setattr(runtime, "_train_all_pending", lambda _learner_id: None)
    monkeypatch.setattr(
        runtime,
        "_snapshot",
        lambda *args, **kwargs: {
            "policy_enabled": True,
            "mode": "local_first",
            "active_version": 1,
            "active_version_id": str(uuid4()),
            "artifact_hash": "frozen",
            "judged_request_count": 300,
            "learner_id": str(learner_id),
        },
    )

    def fake_run(case, *, arm, **kwargs):
        return {
            "run_id": uuid4(),
            "text": "synthetic",
            "routing": {
                "decision_source": "local_router",
                "selected_model": "gpt-5.4-mini",
                "decision_factors": {
                    "task_requirements": dict(case.requirements),
                    "local_confidence": 0.99,
                },
            },
        }

    monkeypatch.setattr(runtime, "_run_workflow", fake_run)
    captured = {}

    def fake_assess(rows, **kwargs):
        captured.update(kwargs)
        return {"verdict": "PASS", "failures": [], "incomplete": [], "metrics": {}}

    monkeypatch.setattr(runtime, "assess_holdout", fake_assess)
    result = runtime._execute_seed(
        cases=cases,
        seed=17,
        ledger=object(),
        user_id=uuid4(),
        organization_id=uuid4(),
        judge_model="gpt-5.4-mini",
        quality_model="gpt-5.4",
        candidate_models=["gpt-4o-mini", "gpt-5.4-mini", "gpt-5.6-sol"],
        quality_callback=lambda *args, **kwargs: {
            arm: {"score": 95.0, "pass": True}
            for arm in ("automatic", "mid", "high")
        },
    )

    expected = {
        case.case_id: {
            "difficulty": case.difficulty,
            "gold": dict(case.requirements),
            "allowed_model_ids": expected_allowed[case.case_id],
        }
        for case in cases
        if case.split == "holdout"
    }
    assert result["verdict"] == "PASS"
    assert captured["expected_cases"] == expected
    assert "expected_ids" not in captured


def test_gold_model_contract_uses_fixed_catalog_matrix_and_has_diversity():
    import scripts.model_routing_verification_runtime as runtime

    holdout = [case for case in _cases() if case.split == "holdout"]
    allowed, diversity = runtime._gold_model_contract(
        holdout,
        candidate_models=["gpt-4o-mini", "gpt-5.4-mini", "gpt-5.6-sol"],
        judge_model="gpt-5.4-mini",
    )

    assert set(allowed) == {case.case_id for case in holdout}
    assert all(isinstance(models, frozenset) and len(models) == 1 for models in allowed.values())
    assert {next(iter(models)) for models in allowed.values()} == {
        "gpt-5.4-mini",
        "gpt-5.6-sol",
    }
    assert diversity == 2


def test_gold_model_contract_fails_closed_for_unknown_catalog_candidate():
    import scripts.model_routing_verification_runtime as runtime

    with pytest.raises(
        runtime.RuntimeVerificationError,
        match="model_selection_contract_unverifiable",
    ):
        runtime._gold_model_contract(
            [_cases()[-1]],
            candidate_models=["synthetic-unknown-model"],
            judge_model="synthetic-unknown-model",
        )


@pytest.mark.parametrize(
    "models",
    [
        ["gpt-5.6-sol"],
        ["gpt-5.4-mini"],
        ["gpt-5.4-mini", "gpt-5.6-sol", "gpt-5.4-mini"],
    ],
)
def test_fixed_quality_baselines_and_candidates_are_preregistered(
    monkeypatch, tmp_path, models
):
    import scripts.model_routing_verification_runtime as runtime

    monkeypatch.setenv("NODEASE_ROUTING_VERIFICATION_ISOLATED_DB", "1")
    with pytest.raises(ValueError, match="candidate_models_invalid"):
        runtime.run_persisted_verification(
            _cases(),
            output_dir=tmp_path,
            ledger=SimpleNamespace(summary=lambda: {}),
            user_id=uuid4(),
            organization_id=uuid4(),
            candidate_models=models,
            seeds=(17,),
        )
