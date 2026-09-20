from copy import deepcopy

import pytest

from scripts.model_routing_verification import (
    artifact_hash,
    assess_holdout,
    paired_score_interval,
    summarize_historical_run,
)


def healthy_rows():
    return [
        {
            "case_id": f"case-{i}",
            "difficulty": difficulty,
            "gold": {"task_complexity": level, "decision_impact": level, "evidence_synthesis": level},
            "predicted": {"task_complexity": level, "decision_impact": level, "evidence_synthesis": level},
            "decision_source": "local_router",
            "model_id": f"model-{difficulty}",
            "quality_pass": True,
            "quality_score": 95,
            "mid_score": 95,
            "high_score": 95,
        }
        for i, (difficulty, level) in enumerate([("low", 0), ("medium", 2), ("high", 3)] * 50)
    ]


def registered_cases(rows=None):
    rows = healthy_rows() if rows is None else rows
    return {
        row["case_id"]: {
            "difficulty": row["difficulty"],
            "gold": deepcopy(row["gold"]),
            "allowed_model_ids": {row["model_id"]},
        }
        for row in rows
    }


def healthy_snapshot(**overrides):
    snapshot = {
        "active_version": 1,
        "artifact_hash": "fixed",
        "judged_request_count": 300,
        "learner_id": "learner-1",
    }
    snapshot.update(overrides)
    return snapshot


def assess(rows, **kwargs):
    return assess_holdout(
        rows,
        expected_cases=kwargs.pop("expected_cases", registered_cases()),
        before=kwargs.pop("before", healthy_snapshot()),
        after=kwargs.pop("after", healthy_snapshot()),
        persisted_activation=kwargs.pop("persisted_activation", True),
        **kwargs,
    )


def test_complete_frozen_local_run_passes():
    assert assess(healthy_rows())["verdict"] == "PASS"


def test_same_model_for_every_case_passes_when_preregistered_as_allowed():
    rows = healthy_rows()
    expected_cases = registered_cases()
    for row in rows:
        row["model_id"] = "shared-model"
    for contract in expected_cases.values():
        contract["allowed_model_ids"] = {"shared-model"}
    assert assess(rows, expected_cases=expected_cases)["verdict"] == "PASS"


def test_single_model_fails_when_preregistered_cases_require_other_models():
    rows = healthy_rows()
    for row in rows:
        row["model_id"] = "weak-model"
    result = assess(rows)
    assert result["verdict"] == "FAIL"
    assert "model_selection_contract" in result["failures"]


def test_missing_model_allowlist_cannot_produce_complete_verdict():
    expected_cases = registered_cases()
    expected_cases["case-0"].pop("allowed_model_ids")
    result = assess(healthy_rows(), expected_cases=expected_cases)
    assert result["verdict"] == "INCOMPLETE"
    assert "model_selection_contract_missing" in result["incomplete"]


def test_model_allowlist_only_applies_when_requirement_prediction_is_correct():
    rows = healthy_rows()
    rows[0]["predicted"]["task_complexity"] = 1
    rows[0]["model_id"] = "outside-model"
    result = assess(rows)
    assert result["verdict"] == "PASS"
    assert "model_selection_contract" not in result["failures"]


def test_all_judge_run_cannot_pass_even_with_perfect_quality():
    rows = healthy_rows()
    for row in rows:
        row["decision_source"] = "runtime_judge"
    result = assess(rows)
    assert result["verdict"] == "FAIL"
    assert "local_coverage" in result["failures"]


def test_missing_or_duplicate_evaluations_are_not_dropped_from_denominator():
    rows = healthy_rows()
    rows[-1] = deepcopy(rows[0])
    assert assess(rows)["verdict"] == "INCOMPLETE"
    rows = healthy_rows()
    rows[0]["quality_score"] = None
    assert assess(rows)["verdict"] == "INCOMPLETE"


def test_artifact_change_invalidates_holdout():
    rows = healthy_rows()
    result = assess(
        rows,
        before=healthy_snapshot(artifact_hash="a"),
        after=healthy_snapshot(artifact_hash="b"),
    )
    assert result["verdict"] == "INCOMPLETE"
    assert "state_changed_during_holdout" in result["incomplete"]


def test_high_risk_underestimation_fails_even_when_average_accuracy_passes():
    rows = healthy_rows()
    rows[2]["predicted"]["decision_impact"] = 2
    result = assess(rows)
    assert result["verdict"] == "FAIL"
    assert "high_risk_underestimation" in result["failures"]


def test_single_prediction_is_detected_without_requiring_uniform_model_shares():
    rows = healthy_rows()
    for row in rows:
        row["predicted"] = dict(rows[0]["gold"])
    assert "prediction_collapse" in assess(rows)["failures"]


def test_missing_persisted_activation_never_passes():
    rows = healthy_rows()
    result = assess(rows, persisted_activation=False)
    assert result["verdict"] == "INCOMPLETE"


@pytest.mark.parametrize("field", ["active_version", "artifact_hash", "judged_request_count", "learner_id"])
def test_persisted_snapshot_requires_backend_identity_fields(field):
    snapshot = healthy_snapshot()
    del snapshot[field]
    result = assess(healthy_rows(), before=snapshot, after=deepcopy(snapshot))
    assert result["verdict"] == "INCOMPLETE"
    assert "persisted_snapshot_invalid" in result["incomplete"]


@pytest.mark.parametrize("field", ["decision_source", "model_id"])
def test_complete_evidence_requires_source_and_model_per_case(field):
    rows = healthy_rows()
    rows[0][field] = None
    result = assess(rows)
    assert result["verdict"] == "INCOMPLETE"
    assert "routing_evidence_missing" in result["incomplete"]


def test_unregistered_or_altered_case_evidence_is_incomplete():
    rows = healthy_rows()
    rows[0]["gold"]["task_complexity"] = 3
    result = assess(rows)
    assert result["verdict"] == "INCOMPLETE"
    assert "case_contract_mismatch" in result["incomplete"]

    rows = healthy_rows()
    rows[0]["difficulty"] = "high"
    result = assess(rows)
    assert result["verdict"] == "INCOMPLETE"
    assert "case_contract_mismatch" in result["incomplete"]

    rows = healthy_rows()
    rows[0]["case_id"] = "invented-case"
    result = assess(rows)
    assert result["verdict"] == "INCOMPLETE"
    assert "missing_or_duplicate_cases" in result["incomplete"]


def test_holdout_without_level_three_cases_is_incomplete():
    rows = healthy_rows()
    for row in rows:
        if row["gold"]["decision_impact"] == 3:
            row["gold"]["decision_impact"] = 2
            row["predicted"]["decision_impact"] = 2
    result = assess(rows, expected_cases=registered_cases(rows))
    assert result["verdict"] == "INCOMPLETE"
    assert "high_risk_cases_missing" in result["incomplete"]


@pytest.mark.parametrize("field", ["quality_score", "mid_score", "high_score"])
def test_boolean_quality_scores_are_not_numeric_evidence(field):
    rows = healthy_rows()
    rows[0][field] = True
    result = assess(rows)
    assert result["verdict"] == "INCOMPLETE"
    assert "quality_evaluation_missing" in result["incomplete"]


@pytest.mark.parametrize("axis", ["task_complexity", "decision_impact", "evidence_synthesis"])
def test_each_constant_prediction_axis_is_named_as_collapse(axis):
    rows = healthy_rows()
    for row in rows:
        row["predicted"][axis] = 2
    result = assess(rows)
    assert result["verdict"] == "FAIL"
    assert f"{axis}_collapse" in result["failures"]


def test_paired_bootstrap_is_reproducible_and_checks_each_baseline():
    assert paired_score_interval([1.0] * 150) == [1.0, 1.0]
    rows = healthy_rows()
    for row in rows:
        row["quality_score"] = 80
    assert "quality_noninferiority_high" in assess(rows)["failures"]


@pytest.mark.parametrize("count", [20, 30])
def test_historical_collapse_is_a_failure_not_a_successful_learning_run(count):
    report = {"runs": [{"arms": {"automatic": {"selected_model": "one", "routing": {"decision_source": "local_router"}}}} for _ in range(count)]}
    summary = summarize_historical_run(report)
    assert summary["local_count"] == count
    assert summary["local_single_model"] is True


def test_hash_ignores_mapping_order_but_detects_weights_change():
    assert artifact_hash({"a": 1, "b": 2}) == artifact_hash({"b": 2, "a": 1})
    assert artifact_hash({"weights": [1]}) != artifact_hash({"weights": [2]})


def test_persisted_cli_dry_run_never_needs_db_or_provider(capsys):
    import json
    from scripts.model_routing_verification import main

    assert main(["--mode", "persisted"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True
    assert result["cases"] == 450
    assert result["candidate_models"] == ["gpt-4o-mini", "gpt-5.4-mini", "gpt-5.6-sol"]


def test_persisted_cli_requires_explicit_subjects_before_provider_or_db(tmp_path):
    from scripts.model_routing_verification import main

    with pytest.raises(SystemExit, match="user-id and organization-id"):
        main(["--execute", "--mode", "persisted", "--output-dir", str(tmp_path)])
    assert list(tmp_path.iterdir()) == []


def test_legacy_cli_verification_switch_dispatches_without_old_runner(capsys):
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, str(root / "scripts/experiment_judge_first_economics_80.py"), "--verify-convergence", "--mode", "persisted"], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert '"dry_run": true' in result.stdout
