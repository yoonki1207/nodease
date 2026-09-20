from copy import deepcopy
from types import SimpleNamespace

from apps.workflow_engine.services.model_routing_incremental_learning import (
    learning_mode_for,
)
from apps.workflow_engine.services.model_routing_learning_batch import (
    ModelRoutingLearningBatchService,
)


def _learning_label(
    index: int,
    *,
    selected_model_id: str,
) -> SimpleNamespace:
    simple = index % 2 == 0
    return SimpleNamespace(
        status="accepted",
        feature_vector=[1.0, 0.0] if simple else [0.0, 1.0],
        encoder_model_id="verification-encoder",
        selected_model_id=selected_model_id,
        candidate_model_ids=["gpt-4.1-mini", "gpt-5.4"],
        confidence=0.95,
        reason_code="requirements_candidate_selected",
        task_requirements={
            "task_complexity": 1 if simple else 3,
            "decision_impact": 0 if simple else 2,
            "evidence_synthesis": 0 if simple else 2,
        },
        routing_feature_hash=f"verification-{index}",
        local_prediction=None,
        local_confidence=None,
        local_distance_score=None,
        local_margin=None,
        learning_processed_at=None,
    )


def _train_with_model_distribution(selected_model_ids: list[str]) -> dict:
    labels = [
        _learning_label(index, selected_model_id=model_id)
        for index, model_id in enumerate(selected_model_ids)
    ]
    return ModelRoutingLearningBatchService.train_labels(
        learner_state={"mode": "judge_first"},
        labels=labels,
        batch_size=len(labels),
    ).learner_state


def test_requirement_artifact_is_invariant_to_selected_model_distribution():
    """모델 선택 쏠림은 모델 ID와 독립적인 요구 수준 head를 바꾸지 않는다."""

    single_model = _train_with_model_distribution(["gpt-4.1-mini"] * 20)
    balanced_models = _train_with_model_distribution(
        ["gpt-4.1-mini" if index % 2 == 0 else "gpt-5.4" for index in range(20)]
    )

    assert single_model["selected_model_counts"] == {"gpt-4.1-mini": 20}
    assert balanced_models["selected_model_counts"] == {
        "gpt-4.1-mini": 10,
        "gpt-5.4": 10,
    }
    assert single_model["candidate_requirement_artifact"] == balanced_models[
        "candidate_requirement_artifact"
    ]


def test_local_first_gate_rejects_prediction_collapse_as_the_only_changed_field():
    """Judge 요구 수준은 다양한데 local 예측만 하나로 줄면 활성화하지 않는다."""

    healthy = {
        "judged_request_count": 100,
        "success_rate": 1.0,
        "schema_pass_rate": 1.0,
        "downstream_success_rate": 1.0,
        "fallback_rate": 0.0,
        "recent_judge_match_rate": 0.80,
        "recent_axis_accuracies": {
            "task_complexity": 0.90,
            "decision_impact": 0.90,
            "evidence_synthesis": 0.90,
        },
        "recent_axis_mean_errors": {
            "task_complexity": 0.20,
            "decision_impact": 0.20,
            "evidence_synthesis": 0.20,
        },
        "recent_judge_label_diversity": 3,
        "recent_local_prediction_diversity": 3,
        "recent_contract_pass_rate": 1.0,
        "recent_evaluation_sample_count": 50,
        "high_risk_underestimation_count": 0,
    }

    collapsed = deepcopy(healthy)
    collapsed["recent_local_prediction_diversity"] = 1

    assert learning_mode_for(**healthy) == "local_first"
    assert learning_mode_for(**collapsed) == "judge_first"
