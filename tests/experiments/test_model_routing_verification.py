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


def assess(rows, **kwargs):
    return assess_holdout(
        rows,
        expected_ids={row["case_id"] for row in healthy_rows()},
        before={"version": 1, "artifact_hash": "fixed", "labels": 300},
        after={"version": 1, "artifact_hash": "fixed", "labels": 300},
        persisted_activation=True,
        **kwargs,
    )


def test_complete_frozen_local_run_passes():
    assert assess(healthy_rows())["verdict"] == "PASS"


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
    result = assess_holdout(rows, expected_ids={r["case_id"] for r in rows}, before={"hash": "a"}, after={"hash": "b"}, persisted_activation=True)
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
    result = assess_holdout(rows, expected_ids={r["case_id"] for r in rows}, before={}, after={}, persisted_activation=False)
    assert result["verdict"] == "INCOMPLETE"


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
