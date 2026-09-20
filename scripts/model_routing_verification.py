"""Strict, payload-free evidence for routing convergence verification.

The offline/standalone provider experiments measure the real encoder and learner.
They deliberately cannot claim persisted activation or end-to-end quality success.
Those claims require the separate PostgreSQL activation test and workflow evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
import subprocess
import uuid
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

AXES = ("task_complexity", "decision_impact", "evidence_synthesis")
SEEDS = (17, 42, 73)
ROOT = Path(__file__).resolve().parents[1]


def artifact_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def paired_score_interval(differences: list[float]) -> list[float]:
    if not differences or not all(math.isfinite(x) for x in differences):
        raise ValueError("finite paired scores are required")
    rng = random.Random(1729)
    means = sorted(statistics.mean(rng.choices(differences, k=len(differences))) for _ in range(2000))
    return [round(means[49], 6), round(means[1949], 6)]


def prediction_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def valid_axis_values(value: Any) -> bool:
        return isinstance(value, dict) and all(
            isinstance(value.get(key), int)
            and not isinstance(value.get(key), bool)
            and 0 <= value[key] <= 3
            for key in AXES
        )

    count = len(rows)
    gold_available = [row for row in rows if valid_axis_values(row.get("gold"))]
    available = [row for row in gold_available if valid_axis_values(row.get("predicted"))]
    accuracy = {key: sum(row["predicted"][key] == row["gold"][key] for row in available) / count if count else 0.0 for key in AXES}
    exact = sum(all(row["predicted"][key] == row["gold"][key] for key in AXES) for row in available)
    high_risk = [row for row in gold_available if row["gold"]["decision_impact"] == 3]
    under = sum(not valid_axis_values(row.get("predicted")) or row["predicted"]["decision_impact"] < 3 for row in high_risk)
    local = sum(row.get("decision_source") == "local_router" for row in rows)
    groups = {difficulty: [row for row in rows if row.get("difficulty") == difficulty] for difficulty in ("low", "medium", "high")}
    gold_axis_diversity = {key: len({row["gold"][key] for row in gold_available}) for key in AXES}
    prediction_axis_diversity = {key: len({row["predicted"][key] for row in available}) for key in AXES}
    return {
        "sample_count": count,
        "prediction_count": len(available),
        "axis_accuracies": accuracy,
        "exact_match_rate": exact / count if count else 0.0,
        "high_risk_count": len(high_risk),
        "high_risk_underestimation_count": under,
        "high_risk_zero_error_upper_95": (1 - 0.05 ** (1 / len(high_risk))) if high_risk and under == 0 else None,
        "gold_diversity": len({tuple(row["gold"][k] for k in AXES) for row in gold_available}),
        "prediction_diversity": len({tuple(row["predicted"][k] for k in AXES) for row in available}),
        "gold_axis_diversity": gold_axis_diversity,
        "prediction_axis_diversity": prediction_axis_diversity,
        "model_distribution": dict(Counter(row.get("model_id") for row in rows if row.get("model_id"))),
        "local_count": local,
        "local_coverage": local / count if count else 0.0,
        "group_local_coverage": {k: sum(r.get("decision_source") == "local_router" for r in group) / len(group) if group else 0.0 for k, group in groups.items()},
    }


def assess_holdout(
    rows: list[dict[str, Any]],
    *,
    expected_cases: dict[str, dict[str, Any]],
    before: dict,
    after: dict,
    persisted_activation: bool,
) -> dict[str, Any]:
    """A complete PASS requires genuine runtime and independent quality evidence."""
    def valid_axis_values(value: Any) -> bool:
        return isinstance(value, dict) and set(value) == set(AXES) and all(
            isinstance(value[key], int)
            and not isinstance(value[key], bool)
            and 0 <= value[key] <= 3
            for key in AXES
        )

    def nonempty_string(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    def valid_snapshot(value: Any) -> bool:
        return (
            isinstance(value, dict)
            and isinstance(value.get("active_version"), int)
            and not isinstance(value.get("active_version"), bool)
            and value["active_version"] > 0
            and nonempty_string(value.get("artifact_hash"))
            and isinstance(value.get("judged_request_count"), int)
            and not isinstance(value.get("judged_request_count"), bool)
            and value["judged_request_count"] > 0
            and nonempty_string(value.get("learner_id"))
        )

    incomplete: list[str] = []
    failures: list[str] = []
    expected_cases_valid = isinstance(expected_cases, dict) and bool(expected_cases) and all(
        nonempty_string(case_id)
        and isinstance(contract, dict)
        and nonempty_string(contract.get("difficulty"))
        and valid_axis_values(contract.get("gold"))
        for case_id, contract in expected_cases.items()
    )
    ids = [row.get("case_id") for row in rows]
    ids_valid = all(nonempty_string(case_id) for case_id in ids)
    unique_ids = ids_valid and len(ids) == len(set(ids))
    if not expected_cases_valid or not unique_ids or set(ids) != set(expected_cases):
        incomplete.append("missing_or_duplicate_cases")
    if not expected_cases_valid or any(
        not isinstance(expected_cases.get(row.get("case_id")), dict)
        or row.get("difficulty") != expected_cases[row["case_id"]].get("difficulty")
        or not valid_axis_values(row.get("gold"))
        or row.get("gold") != expected_cases[row["case_id"]].get("gold")
        for row in rows
    ):
        incomplete.append("case_contract_mismatch")
    if not valid_snapshot(before) or not valid_snapshot(after):
        incomplete.append("persisted_snapshot_invalid")
    if before != after:
        incomplete.append("state_changed_during_holdout")
    if not persisted_activation:
        incomplete.append("persisted_activation_unverified")
    if not rows or any(
        not nonempty_string(row.get("decision_source")) or not nonempty_string(row.get("model_id"))
        for row in rows
    ):
        incomplete.append("routing_evidence_missing")
    metrics = prediction_metrics(rows)
    if metrics["high_risk_count"] == 0:
        incomplete.append("high_risk_cases_missing")
    if metrics["local_coverage"] < 0.70:
        failures.append("local_coverage")
    if any(value < 0.60 for value in metrics["group_local_coverage"].values()):
        failures.append("group_local_coverage")
    if any(value < 0.85 for value in metrics["axis_accuracies"].values()):
        failures.append("axis_accuracy")
    if metrics["exact_match_rate"] < 0.75:
        failures.append("exact_match")
    if metrics["high_risk_underestimation_count"]:
        failures.append("high_risk_underestimation")
    if metrics["gold_diversity"] > 1 and metrics["prediction_diversity"] <= 1:
        failures.append("prediction_collapse")
    for axis in AXES:
        if metrics["gold_axis_diversity"][axis] > 1 and metrics["prediction_axis_diversity"][axis] <= 1:
            failures.append(f"{axis}_collapse")
    quality_keys = ("quality_score", "mid_score", "high_score")
    quality_complete = bool(rows) and all(
        isinstance(row.get("quality_pass"), bool)
        and all(
            isinstance(row.get(key), (int, float))
            and not isinstance(row.get(key), bool)
            and math.isfinite(row[key])
            and 0 <= row[key] <= 100
            for key in quality_keys
        )
        for row in rows
    )
    if not quality_complete:
        incomplete.append("quality_evaluation_missing")
    else:
        metrics["quality_pass_rate"] = sum(row["quality_pass"] for row in rows) / len(rows)
        if metrics["quality_pass_rate"] < 0.90:
            failures.append("quality_pass_rate")
        for baseline in ("mid", "high"):
            interval = paired_score_interval([row["quality_score"] - row[f"{baseline}_score"] for row in rows])
            metrics[f"quality_difference_{baseline}_95_ci"] = interval
            if interval[0] < -5:
                failures.append(f"quality_noninferiority_{baseline}")
    return {"verdict": "INCOMPLETE" if incomplete else "FAIL" if failures else "PASS", "failures": failures, "incomplete": incomplete, "metrics": metrics}


def summarize_historical_run(report: dict[str, Any]) -> dict[str, Any]:
    arms = [r.get("arms", {}).get("automatic", {}) for r in report.get("runs", [])]
    local = [a for a in arms if a.get("routing", {}).get("decision_source") == "local_router"]
    models = Counter(a.get("selected_model") for a in local)
    return {"request_count": len(arms), "local_count": len(local), "local_model_distribution": dict(models), "local_single_model": bool(local) and len(models) == 1}


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def _feature(case):
    from apps.workflow_engine.services.model_router import ModelRouter

    node = SimpleNamespace(
        title="Verification", system_prompt="Use only supplied facts.",
        user_prompt="{{ request }}\n{{ context }}", assistant_prompt="",
        referenced_variables=[SimpleNamespace(name=k, value_selector=["start", k]) for k in ("request", "context")],
    )
    return ModelRouter.learning_feature_text({"start": {"request": case.request, "context": case.context}}, node)


def _optimistic_readiness(state):
    from apps.workflow_engine.services.model_routing_incremental_learning import learning_mode_for

    e = state.get("recent_evaluation", {})
    return learning_mode_for(
        judged_request_count=state.get("judged_request_count", 0),
        recent_judge_match_rate=e.get("judge_match_rate"), recent_axis_accuracies=e.get("axis_accuracies"),
        recent_axis_mean_errors=e.get("axis_mean_errors"), recent_judge_label_diversity=e.get("judge_label_diversity"),
        recent_local_prediction_diversity=e.get("local_prediction_diversity"), recent_contract_pass_rate=e.get("contract_pass_rate"),
        recent_evaluation_sample_count=e.get("sample_count"), high_risk_underestimation_count=e.get("high_risk_underestimation_count"),
        success_rate=1.0, schema_pass_rate=1.0, downstream_success_rate=1.0, fallback_rate=0.0,
    )


def run_classifier_experiment(cases, *, output: Path, live_client=None, ledger=None, judge_model: str | None = None):
    """Real E5/production learner; idealized outcomes never masquerade as DB proof."""
    import torch
    from apps.workflow_engine.services.model_routing_learning_batch import ModelRoutingLearningBatchService
    from apps.workflow_engine.services.model_routing_local_classifier import (
        DEFAULT_MULTILINGUAL_E5_MODEL_ID, DEFAULT_MULTILINGUAL_E5_REVISION,
        MultilingualE5Embedder, MultilingualE5ModelChoiceClassifier, MultilingualE5TaskRequirementClassifier,
    )
    from apps.workflow_engine.services.model_routing_runtime_judge import ModelRoutingRuntimeJudge
    from scripts.model_routing_verification_cases import dataset_hash

    torch.set_num_threads(1)
    embedder = MultilingualE5Embedder(DEFAULT_MULTILINGUAL_E5_MODEL_ID, DEFAULT_MULTILINGUAL_E5_REVISION)
    training = [case for case in cases if case.split == "train"]
    holdout = [case for case in cases if case.split == "holdout"]
    report = {
        "scope": "standalone_live_judge_real_encoder" if live_client else "offline_gold_real_encoder",
        "git_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dataset_hash": dataset_hash(cases), "encoder": DEFAULT_MULTILINGUAL_E5_MODEL_ID,
        "encoder_revision": DEFAULT_MULTILINGUAL_E5_REVISION, "seeds": list(SEEDS),
        "training_count": len(training), "holdout_count": len(holdout),
        "judge_model": judge_model, "runs": [], "verdict": "INCOMPLETE",
        "limitations": ["persisted_activation_not_executed", "workflow_answer_quality_not_evaluated", "training_outcomes_idealized_for_classifier_diagnostic"],
    }
    _write_report(output, report)
    vectors, truths = {}, {}
    # A shared frozen set of labels isolates order sensitivity from Judge randomness.
    # The holdout gold labels are never used in training or passed to the live Judge.
    try:
        for index, case in enumerate(cases, 1):
            feature = _feature(case)
            vector, encoder_id = MultilingualE5ModelChoiceClassifier.vectorize(feature, artifact=None, embedder=embedder)
            vectors[case.case_id] = (vector, encoder_id)
            if case.split == "train":
                if live_client:
                    assessment = ModelRoutingRuntimeJudge.assess_requirements(client=live_client, routing_feature_text=feature)
                    truths[case.case_id] = assessment.task_requirements
                else:
                    truths[case.case_id] = dict(case.requirements)
            if index % 25 == 0:
                print(json.dumps({"stage": "features_and_labels", "completed": index, "total": len(cases), "budget": ledger.summary() if ledger else None}), flush=True)
        report["label_source"] = "live_requirement_judge" if live_client else "preregistered_gold"
        report["label_reuse"] = "same_frozen_training_labels_across_order_seeds"
        for seed in SEEDS:
            ordered = list(training)
            random.Random(seed).shuffle(ordered)
            labels = [SimpleNamespace(
                id=f"{seed}-{case.case_id}", status="accepted", feature_vector=vectors[case.case_id][0],
                encoder_model_id=vectors[case.case_id][1], selected_model_id="diagnostic-only",
                candidate_model_ids=[], confidence=1.0, reason_code="verification",
                task_requirements=truths[case.case_id], local_prediction=None, learning_processed_at=None,
            ) for case in ordered]
            state = {}
            for offset in range(0, len(labels), 10):
                state = ModelRoutingLearningBatchService.train_labels(learner_state=state, labels=labels[offset:offset + 10], batch_size=10).learner_state
            artifact = state["candidate_requirement_artifact"]
            frozen = artifact_hash(artifact)
            rows = []
            for case in holdout:
                prediction = MultilingualE5TaskRequirementClassifier.predict_from_vector(artifact, vector=vectors[case.case_id][0])
                rows.append({
                    "case_id": case.case_id, "difficulty": case.difficulty, "gold": dict(case.requirements),
                    "predicted": dict(prediction.requirements) if prediction else None,
                    "confidence": prediction.confidence if prediction else 0.0,
                    "decision_source": "offline_candidate", "model_id": None,
                })
            metrics = prediction_metrics(rows)
            run = {
                "seed": seed, "artifact_hash": frozen, "frozen_after_holdout": frozen == artifact_hash(artifact),
                "trained_count": artifact.get("trained_example_count"), "recent_prequential_evaluation": state.get("recent_evaluation"),
                "optimistic_readiness": _optimistic_readiness(state), "holdout": metrics, "rows": rows,
                "candidate_confident_coverage": sum(row["confidence"] >= 0.78 for row in rows) / len(rows),
            }
            run["classifier_accuracy_pass"] = all(v >= 0.85 for v in metrics["axis_accuracies"].values()) and metrics["exact_match_rate"] >= 0.75 and metrics["high_risk_underestimation_count"] == 0
            report["runs"].append(run)
            report["classifier_verdict"] = "PASS" if all(r["classifier_accuracy_pass"] for r in report["runs"]) else "FAIL"
            report["budget"] = ledger.summary() if ledger else {"charged_usd": "0", "calls": {"attempted": 0}}
            _write_report(output, report)
            print(json.dumps({"stage": "holdout", "seed": seed, "metrics": metrics, "optimistic_readiness": run["optimistic_readiness"]}), flush=True)
    except Exception as exc:
        report["blocked_reason"] = type(exc).__name__
        report["budget"] = ledger.summary() if ledger else {"charged_usd": "0"}
        _write_report(output, report)
        raise RuntimeError(type(exc).__name__) from None
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--mode", choices=("offline-gold", "live-judge", "persisted"), default="offline-gold")
    parser.add_argument("--judge-model", default="gpt-5.4-mini")
    parser.add_argument("--quality-model", default="gpt-5.4")
    parser.add_argument("--candidate-models", nargs="+", default=["gpt-4o-mini", "gpt-5.4-mini", "gpt-5.6-sol"])
    parser.add_argument("--user-id", type=uuid.UUID)
    parser.add_argument("--organization-id", type=uuid.UUID)
    parser.add_argument("--max-cost-usd", default="30")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports/model-routing/verification/2026-09-21")
    args = parser.parse_args(argv)
    from scripts.model_routing_verification_cases import build_verification_cases, dataset_hash
    cases = build_verification_cases()
    if not args.execute:
        print(json.dumps({"dry_run": True, "mode": args.mode, "dataset_hash": dataset_hash(cases), "cases": len(cases), "splits": dict(Counter(c.split for c in cases)), "seeds": SEEDS, "max_cost_usd": args.max_cost_usd, "candidate_models": args.candidate_models, "persisted_activation": "requires_isolated_database_and_explicit_execution_subject"}, indent=2))
        return 0
    if args.mode == "persisted" and (args.user_id is None or args.organization_id is None):
        raise SystemExit("Persisted mode requires user-id and organization-id for the isolated database.")
    output = args.output_dir / ("persisted-verification.json" if args.mode == "persisted" else f"{args.mode}.json")
    if output.exists():
        raise SystemExit("Output already exists; use a new directory. Unsafe resume is disabled.")
    client = ledger = None
    if args.mode in ("live-judge", "persisted"):
        from decimal import Decimal
        from dotenv import load_dotenv
        from scripts.model_routing_verification_provider import BudgetLedger, env_client
        cap = Decimal(args.max_cost_usd)
        if not cap.is_finite() or not 0 < cap <= 30:
            raise SystemExit("Cost limit must be positive and at most the approved USD 30.")
        load_dotenv(ROOT / ".env", override=False)
        ledger = BudgetLedger(cap)
        if args.mode == "live-judge":
            client = env_client(args.judge_model, ledger)
        else:
            from scripts.model_routing_verification_runtime import run_persisted_verification
            report = run_persisted_verification(cases, output_dir=args.output_dir, ledger=ledger,
                user_id=args.user_id, organization_id=args.organization_id, judge_model=args.judge_model,
                quality_model=args.quality_model, candidate_models=args.candidate_models)
            print(json.dumps({"report": str(output), "overall_verdict": report["verdict"], "budget": report["budget"]}))
            return {"PASS": 0, "FAIL": 2, "INCOMPLETE": 3}[report["verdict"]]
    report = run_classifier_experiment(cases, output=output, live_client=client, ledger=ledger, judge_model=args.judge_model if client else None)
    print(json.dumps({"report": str(output), "classifier_verdict": report.get("classifier_verdict"), "overall_verdict": report["verdict"]}))
    return 2 if report.get("classifier_verdict") == "FAIL" else 3


if __name__ == "__main__":
    raise SystemExit(main())
