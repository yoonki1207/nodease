from __future__ import annotations

import math

import pytest

from tests.load.breakpoint_contract import (
    AssessmentKind,
    Phase,
    PhaseAssessment,
    RequestError,
    RequestSample,
    StopReason,
    choose_boundary_step,
    classify_measurement,
    detect_immediate_stop,
    summarize_measurement,
)


def _sample(
    index: int,
    *,
    latency_s: float = 10.0,
    status_code: int | None = 200,
    error: RequestError | None = None,
    phase: Phase = Phase.MEASURE,
) -> RequestSample:
    started_at_s = float(index)
    return RequestSample(
        scheduled_at_s=started_at_s,
        started_at_s=started_at_s,
        ended_at_s=started_at_s + latency_s,
        phase=phase,
        status_code=status_code,
        error=error,
        latency_s=latency_s,
        run_hash=f"{index:064x}",
    )


def test_measurement_cohort_includes_requests_that_finish_after_phase_end() -> None:
    samples = [_sample(index) for index in range(100)]

    summary = summarize_measurement(
        samples,
        target_rps=1.0,
        measurement_elapsed_s=100.0,
        fixture_seconds=10,
    )

    assert summary.started_count == 100
    assert summary.finalized_count == 100
    assert summary.completed_after_measurement_count == 9
    assert summary.actual_start_rps == 1.0
    assert summary.arrival_valid is True
    assert summary.p95_latency_s == 10.0
    assert summary.p99_latency_s == 10.0


def test_arrival_validity_counts_missed_requests_and_excludes_warmup() -> None:
    samples = [_sample(index) for index in range(95)]
    samples.extend(
        RequestSample(
            scheduled_at_s=float(index),
            started_at_s=None,
            ended_at_s=None,
            phase="measure",
            status_code=None,
            error=RequestError.DROPPED_LATE,
            latency_s=None,
            run_hash=None,
        )
        for index in range(95, 100)
    )
    samples.extend(_sample(index, phase=Phase.WARMUP) for index in range(100, 110))

    summary = summarize_measurement(
        samples,
        target_rps=1.0,
        measurement_elapsed_s=100.0,
        fixture_seconds=10,
    )

    assert summary.scheduled_count == 100
    assert summary.started_count == 95
    assert summary.dropped_count == 5
    assert summary.actual_start_rps == 0.95
    assert summary.attainment_ratio == 0.95
    assert summary.arrival_valid is False


def test_early_stop_uses_frozen_elapsed_but_cannot_pass_with_too_few_samples() -> None:
    summary = summarize_measurement(
        [_sample(index) for index in range(10)],
        target_rps=1.0,
        measurement_elapsed_s=10.0,
        fixture_seconds=10,
    )

    assessment = classify_measurement(summary)

    assert summary.actual_start_rps == 1.0
    assert summary.arrival_valid is True
    assert assessment.kind is AssessmentKind.INSUFFICIENT
    assert assessment.boundary_eligible is False


def test_error_and_latency_thresholds_require_100_finalized_samples() -> None:
    one_error = [_sample(index) for index in range(100)]
    one_error[0] = _sample(
        0,
        status_code=None,
        error=RequestError.TIMEOUT,
        latency_s=10.0,
    )
    passing = summarize_measurement(
        one_error,
        target_rps=1.0,
        measurement_elapsed_s=100.0,
        fixture_seconds=10,
    )
    assert passing.error_rate == 0.01
    assert classify_measurement(passing).kind is AssessmentKind.PASS

    two_errors = list(one_error)
    two_errors[1] = _sample(
        1,
        status_code=503,
        error=None,
        latency_s=10.0,
    )
    error_failure = summarize_measurement(
        two_errors,
        target_rps=1.0,
        measurement_elapsed_s=100.0,
        fixture_seconds=10,
    )
    assert error_failure.error_rate == 0.02
    assert classify_measurement(error_failure).kind is AssessmentKind.FAIL

    slow = [_sample(index, latency_s=15.01 if index < 6 else 10.0) for index in range(100)]
    latency_failure = summarize_measurement(
        slow,
        target_rps=1.0,
        measurement_elapsed_s=100.0,
        fixture_seconds=10,
    )
    assert latency_failure.p95_latency_s == 15.01
    assert classify_measurement(latency_failure).kind is AssessmentKind.FAIL

    io30 = summarize_measurement(
        [_sample(index, latency_s=45.0) for index in range(100)],
        target_rps=1.0,
        measurement_elapsed_s=100.0,
        fixture_seconds=30,
    )
    assert classify_measurement(io30).kind is AssessmentKind.PASS


def test_final_censored_requests_are_reported_and_prevent_a_pass() -> None:
    samples = [_sample(index) for index in range(100)]
    samples.append(
        RequestSample(
            scheduled_at_s=100.0,
            started_at_s=100.0,
            ended_at_s=None,
            phase=Phase.MEASURE,
            status_code=None,
            error=RequestError.CENSORED,
            latency_s=None,
            run_hash=None,
        )
    )
    summary = summarize_measurement(
        samples,
        target_rps=1.01,
        measurement_elapsed_s=100.0,
        fixture_seconds=10,
    )

    assert summary.censored_count == 1
    assert summary.started_count == 101
    assert summary.finalized_count == 100
    assert classify_measurement(summary).kind is AssessmentKind.INSUFFICIENT


@pytest.mark.parametrize(
    ("sample", "expected_reason"),
    (
        (
            RequestSample(
                scheduled_at_s=1.0,
                started_at_s=1.0,
                ended_at_s=11.0,
                phase=Phase.WARMUP,
                status_code=200,
                error=None,
                latency_s=10.0,
                run_hash="a" * 64,
                duplicate_run=True,
            ),
            StopReason.DUPLICATE_RUN,
        ),
        (
            RequestSample(
                scheduled_at_s=1.0,
                started_at_s=1.0,
                ended_at_s=11.0,
                phase=Phase.WARMUP,
                status_code=200,
                error=RequestError.RESULT_MISMATCH,
                latency_s=10.0,
                run_hash="b" * 64,
                marker_matches=False,
            ),
            StopReason.MARKER_MISMATCH,
        ),
    ),
)
def test_integrity_failures_stop_immediately_even_during_warmup(
    sample: RequestSample,
    expected_reason: StopReason,
) -> None:
    decision = detect_immediate_stop([sample], now_s=20.0)
    assert decision is not None
    assert decision.reason is expected_reason


def test_recent_severe_error_window_uses_strict_20_percent_threshold() -> None:
    old_failures = [
        RequestSample(
            scheduled_at_s=float(index),
            started_at_s=float(index),
            ended_at_s=50.0 + index / 100,
            phase=Phase.WARMUP,
            status_code=503,
            error=None,
            latency_s=1.0,
            run_hash=f"{index:064x}",
        )
        for index in range(20)
    ]
    recent = [
        RequestSample(
            scheduled_at_s=80.0 + index / 10,
            started_at_s=80.0 + index / 10,
            ended_at_s=81.0 + index / 10,
            phase=Phase.MEASURE,
            status_code=None if index < 5 else 200,
            error=RequestError.TIMEOUT if index < 5 else None,
            latency_s=1.0,
            run_hash=f"{index + 100:064x}",
        )
        for index in range(21)
    ]

    decision = detect_immediate_stop(old_failures + recent, now_s=100.0)

    assert decision is not None
    assert decision.reason is StopReason.SEVERE_RECENT_ERRORS
    assert decision.observed_count == 21
    assert decision.failure_count == 5

    exactly_twenty_percent = recent[:4] + recent[5:21]
    assert detect_immediate_stop(exactly_twenty_percent, now_s=100.0) is None


@pytest.mark.parametrize(
    ("reason", "kind", "eligible"),
    (
        (StopReason.QUEUE_WAIT_EXCEEDED, AssessmentKind.FAIL, True),
        (StopReason.SERVICE_CRASH, AssessmentKind.SERVICE_ABORTED, True),
        (StopReason.HOST_RESOURCE_LIMIT, AssessmentKind.HOST_LIMIT, False),
        (StopReason.LOADGEN_LIMIT, AssessmentKind.LOADGEN_LIMIT, False),
        (StopReason.OBSERVATION_LOST, AssessmentKind.OBSERVATION_LOST, False),
        (StopReason.TIME_LIMIT, AssessmentKind.INVALID, False),
    ),
)
def test_stop_reasons_preserve_service_and_test_validity_categories(
    reason: StopReason,
    kind: AssessmentKind,
    eligible: bool,
) -> None:
    summary = summarize_measurement(
        [_sample(index) for index in range(10)],
        target_rps=1.0,
        measurement_elapsed_s=10.0,
        fixture_seconds=10,
    )

    assessment = classify_measurement(summary, stop_reason=reason)

    assert assessment.kind is kind
    assert assessment.boundary_eligible is eligible


def test_boundary_bisection_ignores_invalid_and_host_limited_results() -> None:
    assessments = [
        PhaseAssessment(5.0, AssessmentKind.PASS, "passed", True),
        PhaseAssessment(6.25, AssessmentKind.INVALID, "arrival_out_of_range", False),
        PhaseAssessment(6.5, AssessmentKind.HOST_LIMIT, "host_resource_limit", False),
        PhaseAssessment(7.5, AssessmentKind.FAIL, "latency_p95_exceeded", True),
    ]

    decision = choose_boundary_step(assessments, bisections_completed=0)

    assert decision.done is False
    assert decision.passing_rate == 5.0
    assert decision.failing_rate == 7.5
    assert decision.next_rate == 6.25

    attempts_exhausted = choose_boundary_step(assessments, bisections_completed=3)
    assert attempts_exhausted.done is True
    assert attempts_exhausted.reason == "maximum_bisections_reached"

    narrow = choose_boundary_step(
        [
            PhaseAssessment(7.0, AssessmentKind.PASS, "passed", True),
            PhaseAssessment(7.5, AssessmentKind.FAIL, "error_rate_exceeded", True),
        ],
        bisections_completed=1,
    )
    assert narrow.done is True
    assert narrow.reason == "boundary_width_within_10_percent"


def test_request_rows_are_json_safe_and_have_no_raw_payload_field() -> None:
    sample = RequestSample(
        scheduled_at_s=1.0,
        started_at_s=1.1,
        ended_at_s=2.1,
        phase="measure",
        status_code=200,
        error=None,
        latency_s=1.0,
        run_hash="c" * 64,
    )

    serialized = sample.to_dict()

    assert serialized == {
        "scheduled_at_s": 1.0,
        "started_at_s": 1.1,
        "ended_at_s": 2.1,
        "phase": "measure",
        "status_code": 200,
        "error": None,
        "latency_s": 1.0,
        "run_hash": "c" * 64,
        "marker_matches": True,
        "duplicate_run": False,
    }
    assert not ({"payload", "request_body", "response_body"} & serialized.keys())


@pytest.mark.parametrize(
    "overrides",
    (
        {"scheduled_at_s": math.nan},
        {"started_at_s": math.inf},
        {"ended_at_s": 0.5},
        {"latency_s": -1.0},
    ),
)
def test_request_rows_reject_non_finite_and_negative_durations(
    overrides: dict[str, float],
) -> None:
    values = {
        "scheduled_at_s": 1.0,
        "started_at_s": 1.0,
        "ended_at_s": 2.0,
        "phase": Phase.MEASURE,
        "status_code": 200,
        "error": None,
        "latency_s": 1.0,
        "run_hash": "d" * 64,
    }
    values.update(overrides)

    with pytest.raises(ValueError):
        RequestSample(**values)


@pytest.mark.parametrize("elapsed", (0.0, -1.0, math.nan, math.inf))
def test_measurement_rejects_non_positive_or_non_finite_elapsed(elapsed: float) -> None:
    with pytest.raises(ValueError):
        summarize_measurement(
            [],
            target_rps=1.0,
            measurement_elapsed_s=elapsed,
            fixture_seconds=10,
        )
