"""Read-only breakpoint telemetry aggregation and guard process.

Only aggregate counters, durations, booleans, and hashed identifiers are read or
persisted. Request bodies, responses, SQL, URLs, and container logs are outside
this observer's contract.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Sequence

import requests

from tests.load.breakpoint_contract import StopReason


CONTAINER_NAMES = (
    "moduly-postgres",
    "moduly-redis",
    "moduly-gateway",
    "moduly-knowledge-worker",
    "moduly-workflow-engine",
    "moduly-log-system",
    "moduly-log-system-beat",
    "moduly-frontend",
    "moduly-sandbox",
    "moduly-nginx",
    "moduly-proxy",
)
PROMETHEUS_URL = "http://127.0.0.1:9090/api/v1/query"
HEALTH_URL = "http://127.0.0.1:18080/api/v1/health"
MOCK_METRICS_URL = "http://127.0.0.1:18081/metrics"
SAMPLE_INTERVAL_S = 5.0
SUSTAINED_LIMIT_S = 30.0
MAC_CRITICAL_SUSTAINED_LIMIT_S = 10.0
QUEUE_AGE_LIMIT_S = 5.0
QUEUE_DEPTH_LIMIT = 200.0
VM_MEMORY_LIMIT_RATIO = 0.90
MAC_PRESSURE_CRITICAL = 4
_HEX_DIGITS = frozenset("0123456789abcdef")


def _finite_number(value: Any, *, minimum: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    if not math.isfinite(normalized):
        return None
    if minimum is not None and normalized < minimum:
        return None
    return normalized


def _hash_id(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) != 64:
        return None
    if any(character not in _HEX_DIGITS for character in value):
        return None
    return value


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * quantile) - 1)
    return ordered[index]


class EventLedger:
    """Incrementally aggregate safe JSONL events across per-process files."""

    def __init__(self, directory: str | Path, *, since_ts: float | None = None):
        self.directory = Path(directory)
        if since_ts is None:
            self.since_ts = None
        else:
            normalized_since = _finite_number(since_ts, minimum=0.0)
            if normalized_since is None:
                raise ValueError("since_ts must be a non-negative finite number")
            self.since_ts = normalized_since
        self._offsets: dict[Path, int] = {}
        self._tails: dict[Path, bytes] = {}
        self._published: dict[str, float] = {}
        self._task_started: dict[str, float] = {}
        self._task_ended: dict[str, float] = {}
        self._engine_started: dict[str, float] = {}
        self._engine_ended: dict[str, float] = {}
        self._pool_acquire_durations: list[float] = []
        self._probe_ready_count = 0

    def read_new_events(self) -> None:
        if not self.directory.is_dir():
            return
        for path in sorted(self.directory.glob("*.jsonl")):
            self._read_file(path)

    def _read_file(self, path: Path) -> None:
        try:
            size = path.stat().st_size
            offset = self._offsets.get(path, 0)
            if size < offset:
                offset = 0
                self._tails[path] = b""
            with path.open("rb") as stream:
                stream.seek(offset)
                chunk = stream.read()
                self._offsets[path] = stream.tell()
        except OSError:
            return
        if not chunk:
            return
        complete = self._tails.get(path, b"") + chunk
        lines = complete.split(b"\n")
        self._tails[path] = lines.pop()
        for line in lines:
            if not line:
                continue
            try:
                event = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(event, dict):
                self._ingest(event)

    @staticmethod
    def _remember_earliest(target: dict[str, float], key: str, timestamp: float) -> None:
        previous = target.get(key)
        if previous is None or timestamp < previous:
            target[key] = timestamp

    @staticmethod
    def _remember_latest(target: dict[str, float], key: str, timestamp: float) -> None:
        previous = target.get(key)
        if previous is None or timestamp > previous:
            target[key] = timestamp

    def _ingest(self, event: dict[str, Any]) -> None:
        name = event.get("event")
        timestamp = _finite_number(event.get("ts"), minimum=0.0)
        if not isinstance(name, str) or timestamp is None:
            return
        if name == "probe_ready":
            self._probe_ready_count += 1
            return
        if self.since_ts is not None and timestamp < self.since_ts:
            return
        task_hash = _hash_id(event.get("task_hash"))
        if name == "publish" and task_hash is not None:
            self._remember_earliest(self._published, task_hash, timestamp)
        elif name == "task_start" and task_hash is not None:
            self._remember_earliest(self._task_started, task_hash, timestamp)
        elif name == "task_end" and task_hash is not None:
            self._remember_latest(self._task_ended, task_hash, timestamp)
        elif name == "engine_start":
            identity = task_hash or _hash_id(event.get("run_hash"))
            if identity is not None:
                self._remember_earliest(self._engine_started, identity, timestamp)
        elif name == "engine_end":
            identity = task_hash or _hash_id(event.get("run_hash"))
            if identity is not None:
                self._remember_latest(self._engine_ended, identity, timestamp)
        elif name == "pool_acquire":
            duration = _finite_number(event.get("duration"), minimum=0.0)
            if duration is not None:
                self._pool_acquire_durations.append(duration)

    def snapshot(self, *, now: float) -> dict[str, int | float | None]:
        normalized_now = _finite_number(now, minimum=0.0)
        if normalized_now is None:
            raise ValueError("now must be a non-negative finite number")

        pending = {
            task_hash: published_at
            for task_hash, published_at in self._published.items()
            if task_hash not in self._task_started and task_hash not in self._task_ended
        }
        oldest_queued_age = (
            max(0.0, normalized_now - min(pending.values())) if pending else 0.0
        )

        active = 0
        points: list[tuple[float, int]] = []
        for identity, started_at in self._engine_started.items():
            if started_at > normalized_now:
                continue
            ended_at = self._engine_ended.get(identity)
            if (
                ended_at is None
                or ended_at < started_at
                or ended_at > normalized_now
            ):
                active += 1
                effective_end = normalized_now
            else:
                effective_end = min(ended_at, normalized_now)
            points.append((started_at, 1))
            if effective_end >= started_at:
                points.append((effective_end, -1))

        overlap = 0
        maximum_overlap = 0
        for _timestamp, delta in sorted(points, key=lambda item: (item[0], item[1])):
            overlap += delta
            maximum_overlap = max(maximum_overlap, overlap)

        durations = self._pool_acquire_durations
        return {
            "published_tasks": len(self._published),
            "started_tasks": len(self._task_started),
            "ended_tasks": len(self._task_ended),
            "pending_tasks": len(pending),
            "oldest_queued_age_s": oldest_queued_age,
            "engine_active": active,
            "engine_max_overlap": maximum_overlap,
            "pool_acquire_count": len(durations),
            "pool_acquire_p95_s": _percentile(durations, 0.95),
            "pool_acquire_max_s": max(durations, default=None),
            "probe_ready_count": self._probe_ready_count,
        }


@dataclass(frozen=True, slots=True)
class GuardObservation:
    observation_available: bool
    health_ok: bool
    containers_ok: bool
    docker_unchanged: bool
    vm_memory_ratio: float | None
    oldest_queued_age_s: float | None
    workflow_queue_depth: float | None
    mac_pressure_level: int | None = None
    mock_available: bool = True
    mock_error_delta: int = 0
    mock_rejected_delta: int = 0
    mock_counter_reset: bool = False


class GuardState:
    """Evaluate immediate and sustained abort conditions without doing I/O."""

    def __init__(self, *, started_at: float):
        self.started_at = started_at
        self._health_failures = 0
        self._observation_lost_since: float | None = None
        self._queue_age_high_since: float | None = None
        self._queue_depth_high_since: float | None = None
        self._memory_high_since: float | None = None
        self._mac_pressure_critical_since: float | None = None

    @staticmethod
    def _update_since(
        previous: float | None,
        condition: bool | None,
        now: float,
    ) -> float | None:
        if condition is None:
            return previous
        if not condition:
            return None
        return previous if previous is not None else now

    @staticmethod
    def _sustained(since: float | None, now: float) -> bool:
        return since is not None and now - since >= SUSTAINED_LIMIT_S

    def evaluate(self, observation: GuardObservation, *, now: float) -> StopReason | None:
        if not observation.docker_unchanged or not observation.containers_ok:
            return StopReason.SERVICE_CRASH

        self._health_failures = 0 if observation.health_ok else self._health_failures + 1
        if self._health_failures >= 3:
            return StopReason.SERVICE_UNHEALTHY

        if observation.mock_rejected_delta > 0:
            return StopReason.LOADGEN_LIMIT
        if observation.mock_error_delta > 0 or observation.mock_counter_reset:
            return StopReason.OBSERVATION_LOST

        mac_pressure_critical = (
            None
            if observation.mac_pressure_level is None
            else observation.mac_pressure_level == MAC_PRESSURE_CRITICAL
        )
        self._mac_pressure_critical_since = self._update_since(
            self._mac_pressure_critical_since,
            mac_pressure_critical,
            now,
        )
        if (
            self._mac_pressure_critical_since is not None
            and now - self._mac_pressure_critical_since
            >= MAC_CRITICAL_SUSTAINED_LIMIT_S
        ):
            return StopReason.HOST_RESOURCE_LIMIT

        memory_high = (
            None
            if observation.vm_memory_ratio is None
            else observation.vm_memory_ratio >= VM_MEMORY_LIMIT_RATIO
        )
        self._memory_high_since = self._update_since(
            self._memory_high_since,
            memory_high,
            now,
        )
        if self._sustained(self._memory_high_since, now):
            return StopReason.HOST_RESOURCE_LIMIT

        queue_age_high = (
            None
            if observation.oldest_queued_age_s is None
            else observation.oldest_queued_age_s > QUEUE_AGE_LIMIT_S
        )
        queue_depth_high = (
            None
            if observation.workflow_queue_depth is None
            else observation.workflow_queue_depth > QUEUE_DEPTH_LIMIT
        )
        self._queue_age_high_since = self._update_since(
            self._queue_age_high_since,
            queue_age_high,
            now,
        )
        self._queue_depth_high_since = self._update_since(
            self._queue_depth_high_since,
            queue_depth_high,
            now,
        )
        if self._sustained(self._queue_age_high_since, now) or self._sustained(
            self._queue_depth_high_since,
            now,
        ):
            return StopReason.QUEUE_WAIT_EXCEEDED

        self._observation_lost_since = self._update_since(
            self._observation_lost_since,
            not (observation.observation_available and observation.mock_available),
            now,
        )
        if self._sustained(self._observation_lost_since, now):
            return StopReason.OBSERVATION_LOST
        return None


@dataclass(frozen=True, slots=True)
class PrometheusSnapshot:
    available: bool
    collection_success: bool
    redis_success: bool
    loki_success: bool
    collection_age_s: float | None
    container_up_count: int
    container_total: int
    workflow_queue_depth: float | None

    @property
    def containers_ok(self) -> bool:
        return self.container_total == len(CONTAINER_NAMES) and (
            self.container_up_count == len(CONTAINER_NAMES)
        )


def _metric_values(result: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [row for row in result if row.get("metric", {}).get("__name__") == name]


def _metric_scalar(result: list[dict[str, Any]], name: str) -> float:
    rows = _metric_values(result, name)
    if not rows:
        raise ValueError("required metric unavailable")
    value = float(rows[0]["value"][1])
    if not math.isfinite(value):
        raise ValueError("required metric invalid")
    return value


def read_prometheus(session: requests.Session, *, wall_time: float) -> PrometheusSnapshot:
    response = session.get(
        PROMETHEUS_URL,
        params={"query": '{job="nodease"}'},
        timeout=3,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise ValueError("prometheus query unsuccessful")
    result = payload["data"]["result"]
    if not isinstance(result, list):
        raise ValueError("prometheus result invalid")

    collection_success = _metric_scalar(result, "nodease_collection_success") == 1
    redis_success = _metric_scalar(result, "nodease_redis_collection_success") == 1
    loki_success = _metric_scalar(result, "nodease_loki_delivery_success") == 1
    collected_at = _metric_scalar(result, "nodease_collection_timestamp_seconds")
    collection_age = max(0.0, wall_time - collected_at)
    container_values = _metric_values(result, "nodease_container_up")
    up_count = sum(float(row["value"][1]) == 1 for row in container_values)
    queue_values = [
        float(row["value"][1])
        for row in _metric_values(result, "nodease_queue_depth")
        if row.get("metric", {}).get("queue") == "workflow"
    ]
    queue_depth = max(queue_values) if queue_values else None
    available = (
        collection_success
        and redis_success
        and loki_success
        and collection_age < SUSTAINED_LIMIT_S
    )
    return PrometheusSnapshot(
        available=available,
        collection_success=collection_success,
        redis_success=redis_success,
        loki_success=loki_success,
        collection_age_s=collection_age,
        container_up_count=up_count,
        container_total=len(container_values),
        workflow_queue_depth=queue_depth,
    )


@dataclass(frozen=True, slots=True)
class ContainerState:
    running: bool
    restart_count: int
    oom_killed: bool
    started_at: str


@dataclass(frozen=True, slots=True)
class MockMetrics:
    active_requests: int
    max_active_requests: int
    request_count: int
    success_count: int
    error_count: int
    rejected_count: int
    configured_max_concurrency: int


@dataclass(frozen=True, slots=True)
class MockMetricDelta:
    error_delta: int
    rejected_delta: int
    counter_reset: bool


class MockMetricsTracker:
    """Track only fixture counter deltas; never retain request content."""

    def __init__(self) -> None:
        self._previous: MockMetrics | None = None

    def observe(self, current: MockMetrics) -> MockMetricDelta:
        previous = self._previous
        self._previous = current
        if previous is None:
            return MockMetricDelta(0, 0, False)
        counter_reset = any(
            new < old
            for new, old in (
                (current.max_active_requests, previous.max_active_requests),
                (current.request_count, previous.request_count),
                (current.success_count, previous.success_count),
                (current.error_count, previous.error_count),
                (current.rejected_count, previous.rejected_count),
            )
        )
        if counter_reset:
            return MockMetricDelta(0, 0, True)
        return MockMetricDelta(
            error_delta=current.error_count - previous.error_count,
            rejected_delta=current.rejected_count - previous.rejected_count,
            counter_reset=False,
        )


def _nonnegative_int(value: Any, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("mock metric invalid")
    if value < 0 or (positive and value == 0):
        raise ValueError("mock metric invalid")
    return value


def read_mock_metrics(session: requests.Session) -> MockMetrics:
    response = session.get(MOCK_METRICS_URL, timeout=3)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("mock metrics invalid")
    metrics = MockMetrics(
        active_requests=_nonnegative_int(payload.get("active_requests")),
        max_active_requests=_nonnegative_int(payload.get("max_active_requests")),
        request_count=_nonnegative_int(payload.get("request_count")),
        success_count=_nonnegative_int(payload.get("success_count")),
        error_count=_nonnegative_int(payload.get("error_count")),
        rejected_count=_nonnegative_int(payload.get("rejected_count")),
        configured_max_concurrency=_nonnegative_int(
            payload.get("configured_max_concurrency"),
            positive=True,
        ),
    )
    if (
        metrics.active_requests > metrics.configured_max_concurrency
        or metrics.max_active_requests > metrics.configured_max_concurrency
        or metrics.active_requests > metrics.max_active_requests
    ):
        raise ValueError("mock metrics invalid")
    return metrics


def read_container_states() -> dict[str, ContainerState]:
    command = [
        "docker",
        "inspect",
        *CONTAINER_NAMES,
        "--format",
        (
            "{{.Name}}\t{{.State.Running}}\t{{.RestartCount}}\t"
            "{{.State.OOMKilled}}\t{{.State.StartedAt}}"
        ),
    ]
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    states: dict[str, ContainerState] = {}
    for line in completed.stdout.splitlines():
        name, running, restarts, oom_killed, started_at = line.split("\t", 4)
        states[name.removeprefix("/")] = ContainerState(
            running=running == "true",
            restart_count=int(restarts),
            oom_killed=oom_killed == "true",
            started_at=started_at,
        )
    if set(states) != set(CONTAINER_NAMES):
        raise ValueError("container state set incomplete")
    return states


def container_states_unchanged(
    baseline: dict[str, ContainerState],
    current: dict[str, ContainerState],
) -> bool:
    if set(current) != set(baseline):
        return False
    return all(
        state.running
        and not state.oom_killed
        and state.restart_count == baseline[name].restart_count
        and state.started_at == baseline[name].started_at
        for name, state in current.items()
    )


def read_vm_memory_ratio() -> float:
    completed = subprocess.run(
        ["docker", "exec", "moduly-postgres", "cat", "/proc/meminfo"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    values: dict[str, int] = {}
    for line in completed.stdout.splitlines():
        key, separator, remainder = line.partition(":")
        if separator and key in {"MemTotal", "MemAvailable"}:
            values[key] = int(remainder.strip().split()[0])
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", -1)
    if total <= 0 or available < 0 or available > total:
        raise ValueError("memory information unavailable")
    return (total - available) / total


def read_mac_pressure_level() -> int | None:
    """Return Darwin's documented normal/warn/critical level, when readable."""

    if sys.platform != "darwin":
        return None
    try:
        completed = subprocess.run(
            ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        level = int(completed.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return level if level in {1, 2, MAC_PRESSURE_CRITICAL} else None


def health_is_ok(session: requests.Session) -> bool:
    try:
        return session.get(HEALTH_URL, timeout=3).status_code == 200
    except requests.RequestException:
        return False


def _write_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, encoded)
    finally:
        os.close(descriptor)


def _write_stop_reason(path: Path, reason: StopReason) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        content = reason.value.encode("ascii")
        written = 0
        while written < len(content):
            written += os.write(descriptor, content[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, path)
    except FileExistsError:
        pass
    finally:
        temporary.unlink(missing_ok=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Observe breakpoint load-test guards")
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--done-file", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=SAMPLE_INTERVAL_S)
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("--interval must be a positive finite number")
    return args


def run_monitor(args: argparse.Namespace) -> int:
    if args.done_file.exists():
        return 0
    phase_started_at = time.time()
    baseline = read_container_states()
    if any(not state.running or state.oom_killed for state in baseline.values()):
        raise RuntimeError("baseline containers are not healthy")

    session = requests.Session()
    session.trust_env = False
    ledger = EventLedger(args.events, since_ts=phase_started_at)
    mock_tracker = MockMetricsTracker()
    started = time.monotonic()
    guard = GuardState(started_at=started)
    next_tick = started

    while not args.done_file.exists():
        monotonic_now = time.monotonic()
        wall_now = time.time()
        ledger.read_new_events()
        event_snapshot = ledger.snapshot(now=wall_now)

        try:
            prometheus = read_prometheus(session, wall_time=wall_now)
            prometheus_read = True
        except (requests.RequestException, ValueError, KeyError, TypeError, IndexError):
            prometheus = PrometheusSnapshot(False, False, False, False, None, 0, 0, None)
            prometheus_read = False

        try:
            current_states = read_container_states()
            docker_unchanged = container_states_unchanged(baseline, current_states)
            docker_read = True
        except (OSError, subprocess.SubprocessError, ValueError):
            docker_unchanged = True
            docker_read = False

        try:
            memory_ratio = read_vm_memory_ratio()
            memory_read = True
        except (OSError, subprocess.SubprocessError, ValueError):
            memory_ratio = None
            memory_read = False

        mac_pressure_level = read_mac_pressure_level()

        try:
            mock_metrics = read_mock_metrics(session)
            mock_delta = mock_tracker.observe(mock_metrics)
            mock_read = True
        except (requests.RequestException, ValueError, KeyError, TypeError):
            mock_metrics = None
            mock_delta = MockMetricDelta(0, 0, False)
            mock_read = False

        health_ok = health_is_ok(session)
        observation_available = (
            prometheus_read
            and prometheus.available
            and docker_read
            and memory_read
            and mock_read
        )
        observation = GuardObservation(
            observation_available=observation_available,
            health_ok=health_ok,
            containers_ok=prometheus.containers_ok if prometheus_read else True,
            docker_unchanged=docker_unchanged,
            vm_memory_ratio=memory_ratio,
            oldest_queued_age_s=event_snapshot["oldest_queued_age_s"],
            workflow_queue_depth=prometheus.workflow_queue_depth,
            mac_pressure_level=mac_pressure_level,
            mock_available=mock_read,
            mock_error_delta=mock_delta.error_delta,
            mock_rejected_delta=mock_delta.rejected_delta,
            mock_counter_reset=mock_delta.counter_reset,
        )
        reason = guard.evaluate(observation, now=monotonic_now)

        row: dict[str, Any] = {
            "ts": wall_now,
            "elapsed_s": max(0.0, monotonic_now - started),
            **event_snapshot,
            "observation_available": observation_available,
            "collection_success": prometheus.collection_success,
            "redis_collection_success": prometheus.redis_success,
            "loki_delivery_success": prometheus.loki_success,
            "collection_age_s": prometheus.collection_age_s,
            "container_up_count": prometheus.container_up_count,
            "container_total": prometheus.container_total,
            "workflow_queue_depth": prometheus.workflow_queue_depth,
            "docker_unchanged": docker_unchanged,
            "vm_memory_ratio": memory_ratio,
            "mac_pressure_available": mac_pressure_level is not None,
            "mac_pressure_level": mac_pressure_level,
            "mock_available": mock_read,
            "mock_active_requests": (
                mock_metrics.active_requests if mock_metrics is not None else None
            ),
            "mock_max_active_requests": (
                mock_metrics.max_active_requests if mock_metrics is not None else None
            ),
            "mock_request_count": (
                mock_metrics.request_count if mock_metrics is not None else None
            ),
            "mock_error_delta": mock_delta.error_delta,
            "mock_rejected_delta": mock_delta.rejected_delta,
            "mock_counter_reset": mock_delta.counter_reset,
            "health_ok": health_ok,
            "stop_reason": reason.value if reason is not None else None,
        }
        _write_jsonl(args.output, row)
        if reason is not None:
            _write_stop_reason(args.stop_file, reason)
            return 2

        next_tick += args.interval
        remaining = next_tick - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        else:
            next_tick = time.monotonic()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return run_monitor(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CONTAINER_NAMES",
    "ContainerState",
    "EventLedger",
    "GuardObservation",
    "GuardState",
    "MockMetricDelta",
    "MockMetrics",
    "MockMetricsTracker",
    "PrometheusSnapshot",
    "container_states_unchanged",
    "health_is_ok",
    "main",
    "parse_args",
    "read_mac_pressure_level",
    "read_mock_metrics",
    "read_container_states",
    "read_prometheus",
    "read_vm_memory_ratio",
    "run_monitor",
]
