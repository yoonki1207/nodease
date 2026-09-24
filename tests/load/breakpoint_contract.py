"""Pure aggregation and classification contracts for breakpoint load tests.

The structures in this module intentionally accept only timing, status, error, and
hashed run identity data. Request/response bodies and other raw payloads have no
place in the persisted contract.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Sequence


MINIMUM_FINALIZED_SAMPLES = 100
ARRIVAL_RATIO_MINIMUM = 0.95
ARRIVAL_RATIO_MAXIMUM = 1.05
RECENT_WINDOW_SECONDS = 30.0
RECENT_MINIMUM_SAMPLES = 20
RECENT_SEVERE_ERROR_RATIO = 0.20
MAXIMUM_BOUNDARY_BISECTIONS = 3
BOUNDARY_WIDTH_RATIO = 0.10


class Phase(str, Enum):
    WARMUP = "warmup"
    MEASURE = "measure"


class RequestError(str, Enum):
    TIMEOUT = "timeout"
    NETWORK = "network"
    RESULT_MISMATCH = "result_mismatch"
    DROPPED_LATE = "dropped_late"
    SLOTS_EXHAUSTED = "slots_exhausted"
    CENSORED = "censored"
    OTHER = "other"


class StopReason(str, Enum):
    MARKER_MISMATCH = "marker_mismatch"
    DUPLICATE_RUN = "duplicate_run"
    SEVERE_RECENT_ERRORS = "severe_recent_errors"
    QUEUE_WAIT_EXCEEDED = "queue_wait_exceeded"
    SERVICE_UNHEALTHY = "service_unhealthy"
    SERVICE_CRASH = "service_crash"
    HOST_RESOURCE_LIMIT = "host_resource_limit"
    LOADGEN_LIMIT = "loadgen_limit"
    OBSERVATION_LOST = "observation_lost"
    TIME_LIMIT = "time_limit"


class AssessmentKind(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    SERVICE_ABORTED = "service_aborted"
    INSUFFICIENT = "insufficient"
    INVALID = "invalid"
    HOST_LIMIT = "host_limit"
    LOADGEN_LIMIT = "loadgen_limit"
    OBSERVATION_LOST = "observation_lost"


def _finite_number(
    value: float,
    *,
    label: str,
    minimum: float | None = None,
    strictly_positive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{label} must be a finite number")
    if strictly_positive and normalized <= 0:
        raise ValueError(f"{label} must be positive")
    if minimum is not None and normalized < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return normalized


def _optional_finite_number(
    value: float | None,
    *,
    label: str,
    minimum: float | None = None,
) -> float | None:
    if value is None:
        return None
    return _finite_number(value, label=label, minimum=minimum)


@dataclass(frozen=True, slots=True)
class RequestSample:
    scheduled_at_s: float
    started_at_s: float | None
    ended_at_s: float | None
    phase: Phase | str
    status_code: int | None
    error: RequestError | str | None
    latency_s: float | None
    run_hash: str | None
    marker_matches: bool = True
    duplicate_run: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "scheduled_at_s",
            _finite_number(self.scheduled_at_s, label="scheduled_at_s", minimum=0),
        )
        object.__setattr__(
            self,
            "started_at_s",
            _optional_finite_number(
                self.started_at_s,
                label="started_at_s",
                minimum=0,
            ),
        )
        object.__setattr__(
            self,
            "ended_at_s",
            _optional_finite_number(self.ended_at_s, label="ended_at_s", minimum=0),
        )
        object.__setattr__(
            self,
            "latency_s",
            _optional_finite_number(self.latency_s, label="latency_s", minimum=0),
        )
        try:
            object.__setattr__(self, "phase", Phase(self.phase))
        except (TypeError, ValueError) as exc:
            raise ValueError("phase must be warmup or measure") from exc
        if self.error is not None:
            try:
                object.__setattr__(self, "error", RequestError(self.error))
            except (TypeError, ValueError) as exc:
                raise ValueError("error is not a supported request error") from exc

        if self.status_code is not None and (
            type(self.status_code) is not int
            or self.status_code < 0
            or self.status_code > 599
        ):
            raise ValueError("status_code must be an integer from 0 through 599")
        if self.run_hash is not None and (
            not isinstance(self.run_hash, str) or not self.run_hash
        ):
            raise ValueError("run_hash must be a non-empty string or None")
        if type(self.marker_matches) is not bool or type(self.duplicate_run) is not bool:
            raise ValueError("marker_matches and duplicate_run must be booleans")

        if self.started_at_s is None:
            if self.ended_at_s is not None or self.latency_s is not None:
                raise ValueError("an unstarted request cannot have completion timing")
        elif (self.ended_at_s is None) != (self.latency_s is None):
            raise ValueError("ended_at_s and latency_s must be recorded together")
        if (
            self.started_at_s is not None
            and self.ended_at_s is not None
            and self.ended_at_s < self.started_at_s
        ):
            raise ValueError("ended_at_s must not precede started_at_s")

    @property
    def is_finalized(self) -> bool:
        return (
            self.started_at_s is not None
            and self.ended_at_s is not None
            and self.latency_s is not None
            and self.error is not RequestError.CENSORED
        )

    @property
    def is_success(self) -> bool:
        return (
            self.is_finalized
            and self.status_code is not None
            and 200 <= self.status_code < 300
            and self.error is None
            and self.marker_matches
            and not self.duplicate_run
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "scheduled_at_s": self.scheduled_at_s,
            "started_at_s": self.started_at_s,
            "ended_at_s": self.ended_at_s,
            "phase": self.phase.value,
            "status_code": self.status_code,
            "error": self.error.value if self.error is not None else None,
            "latency_s": self.latency_s,
            "run_hash": self.run_hash,
            "marker_matches": self.marker_matches,
            "duplicate_run": self.duplicate_run,
        }


@dataclass(frozen=True, slots=True)
class MeasurementSummary:
    target_rps: float
    measurement_elapsed_s: float
    fixture_seconds: int
    scheduled_count: int
    started_count: int
    finalized_count: int
    completed_after_measurement_count: int
    censored_count: int
    dropped_count: int
    success_count: int
    error_count: int
    timeout_count: int
    http_5xx_count: int
    actual_start_rps: float
    attainment_ratio: float
    arrival_valid: bool
    error_rate: float | None
    p95_latency_s: float | None
    p99_latency_s: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            field: getattr(self, field)
            for field in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class StopDecision:
    reason: StopReason
    observed_count: int
    failure_count: int
    failure_ratio: float

    def to_dict(self) -> dict[str, object]:
        return {
            "reason": self.reason.value,
            "observed_count": self.observed_count,
            "failure_count": self.failure_count,
            "failure_ratio": self.failure_ratio,
        }


@dataclass(frozen=True, slots=True)
class PhaseAssessment:
    target_rps: float
    kind: AssessmentKind | str
    reason: str
    boundary_eligible: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_rps",
            _finite_number(
                self.target_rps,
                label="target_rps",
                strictly_positive=True,
            ),
        )
        try:
            object.__setattr__(self, "kind", AssessmentKind(self.kind))
        except (TypeError, ValueError) as exc:
            raise ValueError("kind is not a supported assessment") from exc
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("reason must be a non-empty string")
        if type(self.boundary_eligible) is not bool:
            raise ValueError("boundary_eligible must be a boolean")

    def to_dict(self) -> dict[str, object]:
        return {
            "target_rps": self.target_rps,
            "kind": self.kind.value,
            "reason": self.reason,
            "boundary_eligible": self.boundary_eligible,
        }


@dataclass(frozen=True, slots=True)
class BoundaryDecision:
    done: bool
    passing_rate: float | None
    failing_rate: float | None
    next_rate: float | None
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            field: getattr(self, field)
            for field in self.__dataclass_fields__
        }


def _nearest_rank(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def summarize_measurement(
    samples: Iterable[RequestSample],
    *,
    target_rps: float,
    measurement_elapsed_s: float,
    fixture_seconds: int,
    measurement_started_at_s: float = 0.0,
) -> MeasurementSummary:
    """Summarize the cohort whose request starts belong to the measure phase."""

    normalized_target = _finite_number(
        target_rps,
        label="target_rps",
        strictly_positive=True,
    )
    normalized_elapsed = _finite_number(
        measurement_elapsed_s,
        label="measurement_elapsed_s",
        strictly_positive=True,
    )
    normalized_start = _finite_number(
        measurement_started_at_s,
        label="measurement_started_at_s",
        minimum=0,
    )
    if type(fixture_seconds) is not int or fixture_seconds not in {10, 30}:
        raise ValueError("fixture_seconds must be 10 or 30")

    measure_samples = [sample for sample in samples if sample.phase is Phase.MEASURE]
    started = [sample for sample in measure_samples if sample.started_at_s is not None]
    finalized = [sample for sample in started if sample.is_finalized]
    censored = [sample for sample in started if not sample.is_finalized]
    dropped = [sample for sample in measure_samples if sample.started_at_s is None]
    successes = [sample for sample in finalized if sample.is_success]
    failures = [sample for sample in finalized if not sample.is_success]
    timeout_count = sum(sample.error is RequestError.TIMEOUT for sample in finalized)
    http_5xx_count = sum(
        sample.status_code is not None and 500 <= sample.status_code < 600
        for sample in finalized
    )
    latencies = [sample.latency_s for sample in finalized if sample.latency_s is not None]
    measurement_ended_at_s = normalized_start + normalized_elapsed
    completed_after = sum(
        sample.ended_at_s is not None and sample.ended_at_s > measurement_ended_at_s
        for sample in finalized
    )

    actual_start_rps = len(started) / normalized_elapsed
    attainment_ratio = actual_start_rps / normalized_target
    arrival_valid = (
        not dropped
        and ARRIVAL_RATIO_MINIMUM <= attainment_ratio <= ARRIVAL_RATIO_MAXIMUM
    )
    error_rate = len(failures) / len(finalized) if finalized else None

    return MeasurementSummary(
        target_rps=normalized_target,
        measurement_elapsed_s=normalized_elapsed,
        fixture_seconds=fixture_seconds,
        scheduled_count=len(measure_samples),
        started_count=len(started),
        finalized_count=len(finalized),
        completed_after_measurement_count=completed_after,
        censored_count=len(censored),
        dropped_count=len(dropped),
        success_count=len(successes),
        error_count=len(failures),
        timeout_count=timeout_count,
        http_5xx_count=http_5xx_count,
        actual_start_rps=actual_start_rps,
        attainment_ratio=attainment_ratio,
        arrival_valid=arrival_valid,
        error_rate=error_rate,
        p95_latency_s=_nearest_rank(latencies, 0.95),
        p99_latency_s=_nearest_rank(latencies, 0.99),
    )


def detect_immediate_stop(
    samples: Iterable[RequestSample],
    *,
    now_s: float,
) -> StopDecision | None:
    """Return integrity or rolling severe-error signals that stop new arrivals."""

    normalized_now = _finite_number(now_s, label="now_s", minimum=0)
    rows = list(samples)
    for sample in rows:
        if not sample.marker_matches or sample.error is RequestError.RESULT_MISMATCH:
            return StopDecision(StopReason.MARKER_MISMATCH, 1, 1, 1.0)
        if sample.duplicate_run:
            return StopDecision(StopReason.DUPLICATE_RUN, 1, 1, 1.0)

    window_start = normalized_now - RECENT_WINDOW_SECONDS
    recent = [
        sample
        for sample in rows
        if sample.is_finalized
        and sample.ended_at_s is not None
        and window_start <= sample.ended_at_s <= normalized_now
    ]
    severe = [
        sample
        for sample in recent
        if sample.error is RequestError.TIMEOUT
        or (
            sample.status_code is not None
            and 500 <= sample.status_code < 600
        )
    ]
    ratio = len(severe) / len(recent) if recent else 0.0
    if len(recent) >= RECENT_MINIMUM_SAMPLES and ratio > RECENT_SEVERE_ERROR_RATIO:
        return StopDecision(
            StopReason.SEVERE_RECENT_ERRORS,
            len(recent),
            len(severe),
            ratio,
        )
    return None


def classify_measurement(
    summary: MeasurementSummary,
    *,
    stop_reason: StopReason | str | None = None,
) -> PhaseAssessment:
    """Classify service failure separately from invalid or constrained runs."""

    normalized_reason: StopReason | None = None
    if stop_reason is not None:
        try:
            normalized_reason = StopReason(stop_reason)
        except (TypeError, ValueError) as exc:
            raise ValueError("stop_reason is not supported") from exc

    if normalized_reason is StopReason.QUEUE_WAIT_EXCEEDED:
        return PhaseAssessment(
            summary.target_rps,
            AssessmentKind.FAIL,
            normalized_reason.value,
            summary.arrival_valid,
        )
    if normalized_reason in {
        StopReason.MARKER_MISMATCH,
        StopReason.DUPLICATE_RUN,
        StopReason.SEVERE_RECENT_ERRORS,
        StopReason.SERVICE_UNHEALTHY,
        StopReason.SERVICE_CRASH,
    }:
        return PhaseAssessment(
            summary.target_rps,
            AssessmentKind.SERVICE_ABORTED,
            normalized_reason.value,
            summary.arrival_valid,
        )
    if normalized_reason is StopReason.HOST_RESOURCE_LIMIT:
        return PhaseAssessment(
            summary.target_rps,
            AssessmentKind.HOST_LIMIT,
            normalized_reason.value,
            False,
        )
    if normalized_reason is StopReason.LOADGEN_LIMIT:
        return PhaseAssessment(
            summary.target_rps,
            AssessmentKind.LOADGEN_LIMIT,
            normalized_reason.value,
            False,
        )
    if normalized_reason is StopReason.OBSERVATION_LOST:
        return PhaseAssessment(
            summary.target_rps,
            AssessmentKind.OBSERVATION_LOST,
            normalized_reason.value,
            False,
        )
    if normalized_reason is StopReason.TIME_LIMIT:
        return PhaseAssessment(
            summary.target_rps,
            AssessmentKind.INVALID,
            normalized_reason.value,
            False,
        )

    if not summary.arrival_valid:
        reason = (
            "scheduled_requests_missed"
            if summary.dropped_count
            else "arrival_out_of_range"
        )
        return PhaseAssessment(
            summary.target_rps,
            AssessmentKind.INVALID,
            reason,
            False,
        )
    if summary.censored_count:
        return PhaseAssessment(
            summary.target_rps,
            AssessmentKind.INSUFFICIENT,
            "final_requests_censored",
            False,
        )
    if summary.finalized_count < MINIMUM_FINALIZED_SAMPLES:
        return PhaseAssessment(
            summary.target_rps,
            AssessmentKind.INSUFFICIENT,
            "insufficient_finalized_samples",
            False,
        )
    if summary.error_rate is not None and summary.error_rate > 0.01:
        return PhaseAssessment(
            summary.target_rps,
            AssessmentKind.FAIL,
            "error_rate_exceeded",
            True,
        )
    latency_limit = 15.0 if summary.fixture_seconds == 10 else 45.0
    if (
        summary.p95_latency_s is not None
        and summary.p95_latency_s > latency_limit
    ):
        return PhaseAssessment(
            summary.target_rps,
            AssessmentKind.FAIL,
            "latency_p95_exceeded",
            True,
        )
    return PhaseAssessment(
        summary.target_rps,
        AssessmentKind.PASS,
        "passed",
        True,
    )


def choose_boundary_step(
    assessments: Iterable[PhaseAssessment],
    *,
    bisections_completed: int,
) -> BoundaryDecision:
    """Choose the midpoint of the validated pass/fail bracket."""

    if type(bisections_completed) is not int or bisections_completed < 0:
        raise ValueError("bisections_completed must be a non-negative integer")
    eligible = [assessment for assessment in assessments if assessment.boundary_eligible]
    passing_rates = [
        assessment.target_rps
        for assessment in eligible
        if assessment.kind is AssessmentKind.PASS
    ]
    failing_rates = [
        assessment.target_rps
        for assessment in eligible
        if assessment.kind in {AssessmentKind.FAIL, AssessmentKind.SERVICE_ABORTED}
    ]
    if not passing_rates or not failing_rates:
        return BoundaryDecision(
            True,
            max(passing_rates, default=None),
            min(failing_rates, default=None),
            None,
            "validated_bracket_unavailable",
        )

    passing_rate = max(passing_rates)
    higher_failures = [rate for rate in failing_rates if rate > passing_rate]
    if not higher_failures:
        return BoundaryDecision(
            True,
            passing_rate,
            min(failing_rates),
            None,
            "validated_bracket_unavailable",
        )
    failing_rate = min(higher_failures)
    if bisections_completed >= MAXIMUM_BOUNDARY_BISECTIONS:
        return BoundaryDecision(
            True,
            passing_rate,
            failing_rate,
            None,
            "maximum_bisections_reached",
        )
    if (failing_rate - passing_rate) / passing_rate <= BOUNDARY_WIDTH_RATIO:
        return BoundaryDecision(
            True,
            passing_rate,
            failing_rate,
            None,
            "boundary_width_within_10_percent",
        )
    return BoundaryDecision(
        False,
        passing_rate,
        failing_rate,
        (passing_rate + failing_rate) / 2,
        "continue_bisection",
    )


__all__ = [
    "AssessmentKind",
    "BoundaryDecision",
    "MeasurementSummary",
    "Phase",
    "PhaseAssessment",
    "RequestError",
    "RequestSample",
    "StopDecision",
    "StopReason",
    "choose_boundary_step",
    "classify_measurement",
    "detect_immediate_stop",
    "summarize_measurement",
]
