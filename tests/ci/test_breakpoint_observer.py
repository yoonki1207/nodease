from __future__ import annotations

import json
from types import SimpleNamespace

from tests.load.breakpoint_contract import StopReason
from tests.load import breakpoint_observer
from tests.load.breakpoint_observer import (
    EventLedger,
    GuardObservation,
    GuardState,
    MockMetrics,
    MockMetricsTracker,
    read_mock_metrics,
)


def _event(event: str, ts: float, **fields: object) -> str:
    return json.dumps({"event": event, "ts": ts, **fields}) + "\n"


def test_event_ledger_is_order_independent_and_deduplicates_task_hashes(
    tmp_path,
) -> None:
    task_a = "a" * 64
    task_b = "b" * 64
    engine_c = "c" * 64
    engine_d = "d" * 64
    engine_e = "e" * 64
    (tmp_path / "worker-2.jsonl").write_text(
        _event("task_start", 13.0, task_hash=task_a)
        + _event("task_end", 20.0, task_hash=task_a, success=True)
        + _event("engine_end", 18.0, task_hash=engine_c, success=True)
        + _event("engine_start", 15.0, task_hash=engine_d)
        + _event("engine_end", 19.0, task_hash=engine_d, success=True)
        + _event("pool_acquire", 21.0, duration=0.3, success=True),
        encoding="utf-8",
    )
    (tmp_path / "worker-1.jsonl").write_text(
        _event("publish", 10.0, task_hash=task_a)
        + _event("publish", 10.5, task_hash=task_a)
        + _event("publish", 11.0, task_hash=task_b)
        + _event("engine_start", 12.0, task_hash=engine_c)
        + _event("engine_start", 25.0, task_hash=engine_e)
        + _event("pool_acquire", 20.0, duration=0.1, success=True)
        + _event("pool_acquire", 22.0, duration=0.2, success=True),
        encoding="utf-8",
    )

    ledger = EventLedger(tmp_path)
    ledger.read_new_events()
    snapshot = ledger.snapshot(now=30.0)

    assert snapshot == {
        "published_tasks": 2,
        "started_tasks": 1,
        "ended_tasks": 1,
        "pending_tasks": 1,
        "oldest_queued_age_s": 19.0,
        "engine_active": 1,
        "engine_max_overlap": 2,
        "pool_acquire_count": 3,
        "pool_acquire_p95_s": 0.3,
        "pool_acquire_max_s": 0.3,
        "probe_ready_count": 0,
    }


def test_event_ledger_keeps_partial_line_until_it_is_complete(tmp_path) -> None:
    task_a = "a" * 64
    task_b = "b" * 64
    path = tmp_path / "worker.jsonl"
    second = _event("publish", 12.0, task_hash=task_b)
    split_at = len(second) // 2
    path.write_text(
        _event("publish", 10.0, task_hash=task_a) + second[:split_at],
        encoding="utf-8",
    )
    ledger = EventLedger(tmp_path)

    ledger.read_new_events()
    assert ledger.snapshot(now=20.0)["published_tasks"] == 1
    with path.open("a", encoding="utf-8") as stream:
        stream.write(second[split_at:])
    ledger.read_new_events()
    assert ledger.snapshot(now=20.0)["published_tasks"] == 2
    ledger.read_new_events()
    assert ledger.snapshot(now=20.0)["published_tasks"] == 2


def test_event_ledger_snapshot_treats_future_end_as_currently_active(tmp_path) -> None:
    task_hash = "a" * 64
    (tmp_path / "worker.jsonl").write_text(
        _event("engine_end", 20.0, task_hash=task_hash, success=True)
        + _event("engine_start", 10.0, task_hash=task_hash),
        encoding="utf-8",
    )
    ledger = EventLedger(tmp_path)
    ledger.read_new_events()

    assert ledger.snapshot(now=15.0)["engine_active"] == 1
    assert ledger.snapshot(now=20.0)["engine_active"] == 0


def test_empty_pending_queue_reports_zero_age_and_resets_queue_guard(tmp_path) -> None:
    task_hash = "a" * 64
    (tmp_path / "worker.jsonl").write_text(
        _event("publish", 1.0, task_hash=task_hash)
        + _event("task_start", 2.0, task_hash=task_hash),
        encoding="utf-8",
    )
    ledger = EventLedger(tmp_path)
    ledger.read_new_events()
    snapshot = ledger.snapshot(now=20.0)
    assert snapshot["pending_tasks"] == 0
    assert snapshot["oldest_queued_age_s"] == 0.0

    guard = GuardState(started_at=0.0)
    assert guard.evaluate(_observation(oldest_queued_age_s=6.0), now=1.0) is None
    assert (
        guard.evaluate(
            _observation(oldest_queued_age_s=snapshot["oldest_queued_age_s"]),
            now=20.0,
        )
        is None
    )
    assert guard.evaluate(_observation(oldest_queued_age_s=6.0), now=31.0) is None


def test_event_ledger_cutoff_excludes_old_phase_events_but_keeps_readiness(
    tmp_path,
) -> None:
    old_task = "a" * 64
    current_task = "b" * 64
    (tmp_path / "worker.jsonl").write_text(
        _event("probe_ready", 5.0)
        + _event("publish", 9.0, task_hash=old_task)
        + _event("pool_acquire", 9.5, duration=4.0, success=True)
        + _event("publish", 10.0, task_hash=current_task)
        + _event("pool_acquire", 10.5, duration=0.2, success=True),
        encoding="utf-8",
    )
    ledger = EventLedger(tmp_path, since_ts=10.0)
    ledger.read_new_events()

    assert ledger.snapshot(now=12.0) == {
        "published_tasks": 1,
        "started_tasks": 0,
        "ended_tasks": 0,
        "pending_tasks": 1,
        "oldest_queued_age_s": 2.0,
        "engine_active": 0,
        "engine_max_overlap": 0,
        "pool_acquire_count": 1,
        "pool_acquire_p95_s": 0.2,
        "pool_acquire_max_s": 0.2,
        "probe_ready_count": 1,
    }


def _observation(**overrides: object) -> GuardObservation:
    values = {
        "observation_available": True,
        "health_ok": True,
        "containers_ok": True,
        "docker_unchanged": True,
        "vm_memory_ratio": 0.5,
        "oldest_queued_age_s": 0.0,
        "workflow_queue_depth": 0.0,
    }
    values.update(overrides)
    return GuardObservation(**values)


def test_guard_requires_sustained_queue_or_memory_pressure() -> None:
    queue_guard = GuardState(started_at=0.0)
    assert queue_guard.evaluate(_observation(oldest_queued_age_s=5.1), now=1.0) is None
    assert queue_guard.evaluate(_observation(oldest_queued_age_s=5.1), now=30.9) is None
    assert (
        queue_guard.evaluate(_observation(oldest_queued_age_s=5.1), now=31.0)
        is StopReason.QUEUE_WAIT_EXCEEDED
    )

    depth_guard = GuardState(started_at=0.0)
    assert depth_guard.evaluate(_observation(workflow_queue_depth=201), now=2.0) is None
    assert (
        depth_guard.evaluate(_observation(workflow_queue_depth=201), now=32.0)
        is StopReason.QUEUE_WAIT_EXCEEDED
    )

    memory_guard = GuardState(started_at=0.0)
    assert memory_guard.evaluate(_observation(vm_memory_ratio=0.9), now=3.0) is None
    assert (
        memory_guard.evaluate(_observation(vm_memory_ratio=0.9), now=33.0)
        is StopReason.HOST_RESOURCE_LIMIT
    )


def test_guard_resets_recovered_pressure_and_tracks_health_failures() -> None:
    guard = GuardState(started_at=0.0)
    assert guard.evaluate(_observation(oldest_queued_age_s=6.0), now=1.0) is None
    assert guard.evaluate(_observation(), now=20.0) is None
    assert guard.evaluate(_observation(oldest_queued_age_s=6.0), now=21.0) is None
    assert guard.evaluate(_observation(oldest_queued_age_s=6.0), now=49.0) is None

    assert guard.evaluate(_observation(health_ok=False), now=50.0) is None
    assert guard.evaluate(_observation(health_ok=False), now=55.0) is None
    assert (
        guard.evaluate(_observation(health_ok=False), now=60.0)
        is StopReason.SERVICE_UNHEALTHY
    )


def test_guard_distinguishes_crash_and_observation_loss() -> None:
    assert (
        GuardState(started_at=0.0).evaluate(
            _observation(docker_unchanged=False),
            now=1.0,
        )
        is StopReason.SERVICE_CRASH
    )

    guard = GuardState(started_at=10.0)
    unavailable = _observation(observation_available=False)
    assert guard.evaluate(unavailable, now=10.0) is None
    assert guard.evaluate(unavailable, now=39.9) is None
    assert guard.evaluate(unavailable, now=40.0) is StopReason.OBSERVATION_LOST


def test_guard_treats_sustained_mac_critical_pressure_as_host_limit() -> None:
    guard = GuardState(started_at=0.0)
    critical = _observation(mac_pressure_level=4)
    assert guard.evaluate(critical, now=2.0) is None
    assert guard.evaluate(critical, now=11.9) is None
    assert guard.evaluate(critical, now=12.0) is StopReason.HOST_RESOURCE_LIMIT

    unavailable = GuardState(started_at=0.0)
    assert unavailable.evaluate(_observation(mac_pressure_level=None), now=60.0) is None


def test_mock_metric_deltas_never_classify_fixture_failure_as_service_failure() -> None:
    tracker = MockMetricsTracker()
    baseline = MockMetrics(0, 1, 1, 1, 0, 0, 300)
    assert tracker.observe(baseline).error_delta == 0
    rejected = tracker.observe(MockMetrics(2, 3, 5, 2, 0, 1, 300))
    assert rejected.rejected_delta == 1
    assert (
        GuardState(started_at=0.0).evaluate(
            _observation(mock_rejected_delta=rejected.rejected_delta),
            now=1.0,
        )
        is StopReason.LOADGEN_LIMIT
    )

    errored = tracker.observe(MockMetrics(0, 3, 6, 2, 1, 1, 300))
    assert errored.error_delta == 1
    assert (
        GuardState(started_at=0.0).evaluate(
            _observation(mock_error_delta=errored.error_delta),
            now=2.0,
        )
        is StopReason.OBSERVATION_LOST
    )


def test_mock_counter_reset_and_unavailability_are_observation_loss() -> None:
    tracker = MockMetricsTracker()
    tracker.observe(MockMetrics(0, 10, 20, 20, 0, 0, 300))
    reset = tracker.observe(MockMetrics(0, 1, 1, 1, 0, 0, 300))
    assert reset.counter_reset is True
    assert (
        GuardState(started_at=0.0).evaluate(
            _observation(mock_counter_reset=True),
            now=1.0,
        )
        is StopReason.OBSERVATION_LOST
    )

    guard = GuardState(started_at=0.0)
    unavailable = _observation(mock_available=False)
    assert guard.evaluate(unavailable, now=1.0) is None
    assert guard.evaluate(unavailable, now=31.0) is StopReason.OBSERVATION_LOST


def test_mock_metrics_reader_accepts_only_safe_aggregate_shape() -> None:
    payload = {
        "active_requests": 2,
        "max_active_requests": 5,
        "request_count": 10,
        "success_count": 8,
        "error_count": 0,
        "rejected_count": 0,
        "configured_max_concurrency": 300,
        "ignored_raw_field": "must-not-be-retained",
    }

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return payload

    class Session:
        def get(self, url, *, timeout):
            assert url == breakpoint_observer.MOCK_METRICS_URL
            assert timeout == 3
            return Response()

    assert read_mock_metrics(Session()) == MockMetrics(2, 5, 10, 8, 0, 0, 300)


def test_mac_pressure_reader_accepts_documented_levels_only(monkeypatch) -> None:
    monkeypatch.setattr(breakpoint_observer.sys, "platform", "darwin")
    monkeypatch.setattr(
        breakpoint_observer.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="4\n"),
    )
    assert breakpoint_observer.read_mac_pressure_level() == 4

    monkeypatch.setattr(
        breakpoint_observer.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="3\n"),
    )
    assert breakpoint_observer.read_mac_pressure_level() is None


def test_stop_reason_publish_is_atomic_and_preserves_first_writer(tmp_path) -> None:
    path = tmp_path / "stop"
    breakpoint_observer._write_stop_reason(path, StopReason.LOADGEN_LIMIT)
    assert path.read_text(encoding="ascii") == StopReason.LOADGEN_LIMIT.value
    assert list(tmp_path.glob("*.tmp")) == []

    breakpoint_observer._write_stop_reason(path, StopReason.SERVICE_CRASH)
    assert path.read_text(encoding="ascii") == StopReason.LOADGEN_LIMIT.value
    assert list(tmp_path.glob("*.tmp")) == []
