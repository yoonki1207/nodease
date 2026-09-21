"""Pure data helpers used by the Nodease Locust scenarios."""

from __future__ import annotations

import copy
from collections import deque
from collections.abc import Iterable
from typing import Generic, TypeVar


T = TypeVar("T")


class ScenarioDataError(ValueError):
    """Stable scenario error that does not include response or secret values."""


class RuntimeUserPool(Generic[T]):
    """Assign each prepared UI account to at most one local Locust user."""

    def __init__(self, users: Iterable[T]) -> None:
        self._source = tuple(users)
        self._available: deque[T] = deque(self._source)

    def reset(self) -> None:
        self._available = deque(self._source)

    def acquire(self) -> T:
        try:
            return self._available.popleft()
        except IndexError:
            raise ScenarioDataError("load_test.ui_account_pool_exhausted") from None


def build_draft_save_payload(draft: object) -> dict[str, object]:
    """Build a same-graph CAS write from one authenticated draft response."""

    if not isinstance(draft, dict):
        raise ScenarioDataError("load_test.draft_shape_invalid")
    nodes = draft.get("nodes")
    edges = draft.get("edges")
    viewport = draft.get("viewport")
    graph_hash = draft.get("graph_hash")
    updated_at = draft.get("updated_at")
    if (
        not isinstance(nodes, list)
        or not isinstance(edges, list)
        or not isinstance(viewport, dict)
        or not isinstance(graph_hash, str)
        or len(graph_hash) != 64
        or not isinstance(updated_at, str)
        or not updated_at
    ):
        raise ScenarioDataError("load_test.draft_shape_invalid")

    payload: dict[str, object] = {
        "nodes": copy.deepcopy(nodes),
        "edges": copy.deepcopy(edges),
        "viewport": copy.deepcopy(viewport),
        "expected_graph_hash": graph_hash,
        "expected_updated_at": updated_at,
    }
    features = draft.get("features")
    if isinstance(features, dict):
        payload["features"] = copy.deepcopy(features)
    return payload


def _has_expected_provider_free_answer(value: object, marker: str) -> bool:
    return (
        isinstance(value, dict)
        and value.get("answer_text") == f"Nodease load response: {marker}"
    )


def deployment_result_error(body: object, marker: str) -> str | None:
    """Validate an API deployment response and return only a stable error code."""

    if not isinstance(body, dict):
        return "api.response_shape_invalid"
    if body.get("status") != "success":
        return "api.workflow_failed"
    run_id = body.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return "api.run_id_missing"
    if not _has_expected_provider_free_answer(body.get("results"), marker):
        return "api.result_mismatch"
    return None


def execution_result_error(body: object, marker: str) -> str | None:
    """Validate the authenticated builder execute response."""

    if not isinstance(body, dict):
        return "ui.response_shape_invalid"
    if not _has_expected_provider_free_answer(
        body.get("answer-load-test"), marker
    ):
        return "ui.result_mismatch"
    return None


def pacing_wait_seconds(
    *,
    period_seconds: float,
    last_start: float | None,
    now: float,
) -> float:
    """Keep one worker on a start-to-start period after its initial phase jitter."""

    if period_seconds <= 0:
        raise ValueError("period_seconds must be positive")
    if last_start is None:
        return period_seconds
    return max(0.0, period_seconds - max(0.0, now - last_start))


__all__ = [
    "RuntimeUserPool",
    "ScenarioDataError",
    "build_draft_save_payload",
    "deployment_result_error",
    "execution_result_error",
    "pacing_wait_seconds",
]
