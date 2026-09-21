"""Pure workload planning contracts with no Locust runtime dependency."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping


STABLE_METRIC_NAMES: Mapping[str, str] = MappingProxyType(
    {
        "ui_auth_login": "ui.auth.login",
        "ui_apps_list": "ui.apps.list",
        "ui_workflow_read": "ui.workflow.read",
        "ui_workflow_draft_read": "ui.workflow.draft.read",
        "ui_workflow_draft_save": "ui.workflow.draft.save",
        "ui_workflow_execute": "ui.workflow.execute",
        "ui_trace_lookup": "ui.trace.lookup",
        "api_deployment_run": "api.deployment.run",
    }
)
REQUEST_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True, slots=True)
class MixedFixedCounts:
    ui_fixed_count: int
    api_fixed_count: int

    @property
    def total_users(self) -> int:
        return self.ui_fixed_count + self.api_fixed_count


class RequestStartRateTracker:
    """Measure starts only across the active post-warmup test window."""

    def __init__(self) -> None:
        self.reset()

    def reset(
        self,
        *,
        warmup_seconds: float = 0.0,
        now: float | None = None,
    ) -> None:
        current = time.monotonic() if now is None else now
        self.started = 0
        self.window_started_at = current + max(0.0, warmup_seconds)
        self.window_stopped_at: float | None = None

    def mark_start(self, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        if self.window_stopped_at is None and current >= self.window_started_at:
            self.started += 1

    def freeze(self, *, now: float | None = None) -> None:
        if self.window_stopped_at is None:
            self.window_stopped_at = time.monotonic() if now is None else now

    def achieved_rps(self, *, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        window_end = (
            self.window_stopped_at
            if self.window_stopped_at is not None
            else current
        )
        duration = max(window_end - self.window_started_at, 0.001)
        return self.started / duration


class LoadStartupTracker:
    """Preserve startup success/failure independently from Locust stats resets."""

    def __init__(self) -> None:
        self.reset(expected_ui=0, expected_api=0)

    def reset(self, *, expected_ui: int, expected_api: int) -> None:
        if min(expected_ui, expected_api) < 0:
            raise ValueError("expected startup counts must be non-negative")
        self.expected_ui = expected_ui
        self.expected_api = expected_api
        self.ui_successes: set[str] = set()
        self.api_successes = 0
        self.failures = 0

    def mark_ui_success(self, identifier: str) -> None:
        self.ui_successes.add(identifier)

    def mark_api_success(self) -> None:
        self.api_successes += 1

    def mark_failure(self) -> None:
        self.failures += 1

    def is_complete(self) -> bool:
        return (
            len(self.ui_successes) + self.api_successes + self.failures
            >= self.expected_ui + self.expected_api
        )

    def is_valid(self) -> bool:
        return (
            self.failures == 0
            and len(self.ui_successes) == self.expected_ui
            and self.api_successes == self.expected_api
        )


def _positive_finite_number(value: float, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a positive finite number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{label} must be a positive finite number")
    return normalized


def minimum_pacing_workers(
    target_rps: float,
    latency_budget_seconds: float,
    headroom: float = 1.5,
) -> int:
    """Return the worker floor for the bounded arrival-rate approximation."""

    normalized_rps = _positive_finite_number(target_rps, label="target_rps")
    normalized_latency = _positive_finite_number(
        latency_budget_seconds,
        label="latency_budget_seconds",
    )
    normalized_headroom = _positive_finite_number(headroom, label="headroom")
    if normalized_headroom < 1:
        raise ValueError("headroom must be at least 1")
    return max(
        1,
        math.ceil(normalized_rps * normalized_latency * normalized_headroom),
    )


def classify_achieved_rate(
    target_rps: float,
    achieved_rps: float,
    minimum_ratio: float = 0.95,
) -> Literal["valid", "invalid"]:
    """Classify a run from request-start rate, not completion throughput."""

    normalized_target = _positive_finite_number(target_rps, label="target_rps")
    if isinstance(achieved_rps, bool) or not isinstance(achieved_rps, (int, float)):
        raise ValueError("achieved_rps must be a non-negative finite number")
    normalized_achieved = float(achieved_rps)
    if not math.isfinite(normalized_achieved) or normalized_achieved < 0:
        raise ValueError("achieved_rps must be a non-negative finite number")
    normalized_ratio = _positive_finite_number(
        minimum_ratio,
        label="minimum_ratio",
    )
    if normalized_ratio > 1:
        raise ValueError("minimum_ratio must not exceed 1")
    if normalized_achieved >= normalized_target * normalized_ratio:
        return "valid"
    return "invalid"


def build_mixed_fixed_counts(
    *,
    ui_users: int,
    api_workers: int,
) -> MixedFixedCounts:
    """Keep human-session and API-arrival users as exact Locust class counts."""

    if type(ui_users) is not int or ui_users <= 0:
        raise ValueError("ui_users must be a positive integer")
    if type(api_workers) is not int or api_workers <= 0:
        raise ValueError("api_workers must be a positive integer")
    return MixedFixedCounts(
        ui_fixed_count=ui_users,
        api_fixed_count=api_workers,
    )


__all__ = [
    "MixedFixedCounts",
    "LoadStartupTracker",
    "RequestStartRateTracker",
    "REQUEST_TIMEOUT_SECONDS",
    "STABLE_METRIC_NAMES",
    "build_mixed_fixed_counts",
    "classify_achieved_rate",
    "minimum_pacing_workers",
]
