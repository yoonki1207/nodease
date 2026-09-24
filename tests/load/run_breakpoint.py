"""Run one bounded arrival-rate breakpoint phase against the local stack."""

from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import fields
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import threading
import time
from typing import Callable, Sequence
import uuid

import requests

from tests.load.breakpoint_contract import (
    ARRIVAL_RATIO_MAXIMUM,
    ARRIVAL_RATIO_MINIMUM,
    AssessmentKind,
    MeasurementSummary,
    Phase,
    PhaseAssessment,
    RequestError,
    RequestSample,
    StopReason,
    classify_measurement,
    detect_immediate_stop,
    summarize_measurement,
)
from tests.load.config import DEFAULT_TARGET_HOST, RuntimeManifest, load_runtime_manifest
from tests.load.scenario_common import deployment_result_error


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "tests/load/.env.runtime.local.json"
MOCK_TARGET = "http://127.0.0.1:18081/delay"
DEFAULT_MAX_INFLIGHT = 300
REQUEST_DEADLINE_SECONDS = 60.0
ARRIVAL_LATE_SECONDS = 0.25
DRAIN_SECONDS = 60.0
STATUS_SECONDS = 30.0
FIXTURE_SECONDS = 10
_REQUEST_KEYS = {field.name for field in fields(RequestSample)}
_WARMUP_BOUNDARY_REASONS = frozenset(
    {
        StopReason.MARKER_MISMATCH,
        StopReason.DUPLICATE_RUN,
        StopReason.SEVERE_RECENT_ERRORS,
        StopReason.QUEUE_WAIT_EXCEEDED,
        StopReason.SERVICE_UNHEALTHY,
        StopReason.SERVICE_CRASH,
    }
)


def session() -> requests.Session:
    client = requests.Session()
    client.trust_env = False
    return client


def _positive_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive finite number") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one local breakpoint arrival-rate phase"
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--rate", required=True, type=_positive_float)
    parser.add_argument("--warmup", required=True, type=_positive_float)
    parser.add_argument("--duration", required=True, type=_positive_float)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--stop-file", type=Path)
    return parser.parse_args(argv)


def arrival_failure(
    due_s: float,
    now_s: float,
    slot_available: bool,
) -> StopReason | None:
    if now_s - due_s > ARRIVAL_LATE_SECONDS or not slot_available:
        return StopReason.LOADGEN_LIMIT
    return None


def phase_for_start(started_at_s: float, *, warmup_s: float) -> Phase:
    return Phase.WARMUP if started_at_s < warmup_s else Phase.MEASURE


def measurement_elapsed(
    *,
    stopped_at_s: float,
    warmup_s: float,
    duration_s: float,
) -> float:
    return min(duration_s, max(0.0, stopped_at_s - warmup_s))


def arrival_window(
    samples: Sequence[RequestSample],
    *,
    target_rps: float,
    measured_s: float,
    stopped_at_s: float,
) -> dict[str, object]:
    before_measurement = measured_s == 0.0
    selected_phase = Phase.WARMUP if before_measurement else Phase.MEASURE
    selected = [sample for sample in samples if sample.phase is selected_phase]
    elapsed_s = stopped_at_s if before_measurement else measured_s
    started_count = sum(sample.started_at_s is not None for sample in selected)
    dropped_count = sum(sample.started_at_s is None for sample in selected)
    actual_start_rps = started_count / elapsed_s if elapsed_s > 0 else 0.0
    attainment_ratio = actual_start_rps / target_rps
    valid = (
        elapsed_s > 0
        and dropped_count == 0
        and ARRIVAL_RATIO_MINIMUM <= attainment_ratio <= ARRIVAL_RATIO_MAXIMUM
    )
    return {
        "scope": (
            "warmup_if_aborted_before_measurement"
            if before_measurement
            else "measurement"
        ),
        "elapsed_s": elapsed_s,
        "scheduled_count": len(selected),
        "started_count": started_count,
        "dropped_count": dropped_count,
        "actual_start_rps": actual_start_rps,
        "attainment_ratio": attainment_ratio,
        "valid": valid,
    }


def write_request_jsonl(path: Path, samples: Sequence[RequestSample]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for sample in samples:
            row = sample.to_dict()
            if set(row) != _REQUEST_KEYS:
                raise RuntimeError("unsafe request sample shape")
            stream.write(json.dumps(row, sort_keys=True, separators=(",", ":")))
            stream.write("\n")


def read_stop_reason(path: Path | None) -> StopReason | None:
    if path is None or not path.exists():
        return None
    try:
        return StopReason(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError, ValueError):
        return StopReason.OBSERVATION_LOST


def api_response_result(
    body: object,
    marker: str,
) -> tuple[RequestError | None, str | None, bool]:
    contract_error = deployment_result_error(body, marker)
    if contract_error is not None:
        marker_matches = contract_error != "api.result_mismatch"
        error = (
            RequestError.RESULT_MISMATCH
            if not marker_matches
            else RequestError.OTHER
        )
        return error, None, marker_matches
    assert isinstance(body, dict)
    run_id = body["run_id"]
    return None, hashlib.sha256(run_id.encode("utf-8")).hexdigest(), True


def mock_response_result(
    body: object,
    marker: str,
) -> tuple[RequestError | None, bool]:
    marker_matches = (
        isinstance(body, dict)
        and body.get("marker") == marker
        and body.get("answer_text") == f"Nodease load response: {marker}"
    )
    if not marker_matches:
        return RequestError.RESULT_MISMATCH, False
    return None, True


class BreakpointRun:
    def __init__(
        self,
        output: Path,
        *,
        rate: float,
        warmup_s: float,
        duration_s: float,
        mock: bool,
        stop_file: Path | None = None,
        max_inflight: int = DEFAULT_MAX_INFLIGHT,
        request_deadline_s: float = REQUEST_DEADLINE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.output = output
        self.rate = rate
        self.warmup_s = warmup_s
        self.duration_s = duration_s
        self.mock = mock
        self.stop_file = stop_file
        self.max_inflight = max_inflight
        self.request_deadline_s = request_deadline_s
        self.clock = clock
        self.manifest: RuntimeManifest | None = None
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.stop_reason: StopReason | None = None
        self.stopped_at_s: float | None = None
        self.samples: list[RequestSample] = []
        self.inflight: dict[float, tuple[float, Phase]] = {}
        self.accept_results = True
        self.seen_run_hashes: set[str] = set()
        self.slots = threading.BoundedSemaphore(max_inflight)
        self.clients = threading.local()
        self.anchor = 0.0
        self.phase_anchor_utc = ""
        self.next_status_s = STATUS_SECONDS

    def _elapsed(self) -> float:
        return self.clock() - self.anchor

    def abort(self, reason: StopReason) -> None:
        with self.lock:
            if self.stop_reason is not None:
                return
            self.stop_reason = reason
            self.stopped_at_s = self._elapsed()
        self.stop.set()

    def _append(self, sample: RequestSample) -> None:
        with self.lock:
            self.samples.append(sample)
            snapshot = list(self.samples)
        decision = detect_immediate_stop(snapshot, now_s=self._elapsed())
        if decision is not None:
            self.abort(decision.reason)

    def _request(self, due_s: float) -> None:
        started_at_s = self._elapsed()
        phase = phase_for_start(started_at_s, warmup_s=self.warmup_s)
        if started_at_s - due_s > ARRIVAL_LATE_SECONDS:
            self._append(
                RequestSample(
                    scheduled_at_s=due_s,
                    started_at_s=None,
                    ended_at_s=None,
                    phase=phase,
                    status_code=None,
                    error=RequestError.DROPPED_LATE,
                    latency_s=None,
                    run_hash=None,
                )
            )
            self.slots.release()
            self.abort(StopReason.LOADGEN_LIMIT)
            return
        with self.lock:
            if not self.accept_results:
                self.slots.release()
                return
            self.inflight[due_s] = (started_at_s, phase)
        marker = "bp-" + uuid.uuid4().hex
        status_code: int | None = None
        error: RequestError | None = None
        run_hash: str | None = None
        marker_matches = True
        duplicate_run = False
        try:
            if not hasattr(self.clients, "client"):
                self.clients.client = session()
            client: requests.Session = self.clients.client
            if self.mock:
                response = client.get(
                    MOCK_TARGET,
                    params={"seconds": FIXTURE_SECONDS, "marker": marker},
                    timeout=self.request_deadline_s,
                    allow_redirects=False,
                )
            else:
                assert self.manifest is not None
                response = client.post(
                    DEFAULT_TARGET_HOST
                    + "/api/v1/run/"
                    + self.manifest.api.deployment_slug,
                    headers={
                        "Authorization": "Bearer " + self.manifest.api.auth_token
                    },
                    json={"inputs": {"message": marker}},
                    timeout=self.request_deadline_s,
                    allow_redirects=False,
                )
            status_code = response.status_code
            if status_code != 200:
                error = RequestError.OTHER
            else:
                try:
                    body = response.json()
                except ValueError:
                    error = RequestError.OTHER
                else:
                    if self.mock:
                        error, marker_matches = mock_response_result(body, marker)
                    else:
                        error, run_hash, marker_matches = api_response_result(body, marker)
        except requests.Timeout:
            error = RequestError.TIMEOUT
        except requests.RequestException:
            error = RequestError.NETWORK
        except Exception:
            error = RequestError.OTHER
        ended_at_s = self._elapsed()
        with self.lock:
            if run_hash is not None:
                duplicate_run = run_hash in self.seen_run_hashes
                self.seen_run_hashes.add(run_hash)
            self.inflight.pop(due_s, None)
            if self.accept_results:
                self.samples.append(
                    RequestSample(
                        scheduled_at_s=due_s,
                        started_at_s=started_at_s,
                        ended_at_s=ended_at_s,
                        phase=phase,
                        status_code=status_code,
                        error=error,
                        latency_s=ended_at_s - started_at_s,
                        run_hash=run_hash,
                        marker_matches=marker_matches,
                        duplicate_run=duplicate_run,
                    )
                )
                snapshot = list(self.samples)
            else:
                snapshot = None
        self.slots.release()
        if snapshot is not None:
            decision = detect_immediate_stop(snapshot, now_s=self._elapsed())
            if decision is not None:
                self.abort(decision.reason)

    def _censor_inflight(self) -> None:
        with self.lock:
            self.accept_results = False
            for due_s, (started_at_s, phase) in self.inflight.items():
                self.samples.append(
                    RequestSample(
                        scheduled_at_s=due_s,
                        started_at_s=started_at_s,
                        ended_at_s=None,
                        phase=phase,
                        status_code=None,
                        error=RequestError.CENSORED,
                        latency_s=None,
                        run_hash=None,
                    )
                )
            self.inflight.clear()

    def _print_status_if_due(self) -> None:
        elapsed = self._elapsed()
        if elapsed < self.next_status_s:
            return
        with self.lock:
            samples = list(self.samples)
            inflight_count = len(self.inflight)
        print(
            json.dumps(
                {
                    "event": "status",
                    "elapsed_s": round(elapsed, 3),
                    "started": (
                        sum(sample.started_at_s is not None for sample in samples)
                        + inflight_count
                    ),
                    "finalized": sum(sample.is_finalized for sample in samples),
                    "dropped": sum(sample.started_at_s is None for sample in samples),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        while self.next_status_s <= elapsed:
            self.next_status_s += STATUS_SECONDS

    def _poll_external_stop(self) -> None:
        reason = read_stop_reason(self.stop_file)
        if reason is not None:
            self.abort(reason)

    def _wait_until(self, due_s: float) -> bool:
        while not self.stop.is_set():
            self._poll_external_stop()
            self._print_status_if_due()
            remaining = due_s - self._elapsed()
            if remaining <= 0:
                return True
            self.stop.wait(min(remaining, 0.25))
        return False

    def _record_dropped(
        self,
        due_s: float,
        *,
        error: RequestError = RequestError.DROPPED_LATE,
    ) -> None:
        phase = Phase.WARMUP if due_s < self.warmup_s else Phase.MEASURE
        self._append(
            RequestSample(
                scheduled_at_s=due_s,
                started_at_s=None,
                ended_at_s=None,
                phase=phase,
                status_code=None,
                error=error,
                latency_s=None,
                run_hash=None,
            )
        )

    def _empty_summary(self) -> MeasurementSummary:
        return MeasurementSummary(
            target_rps=self.rate,
            measurement_elapsed_s=0.0,
            fixture_seconds=FIXTURE_SECONDS,
            scheduled_count=0,
            started_count=0,
            finalized_count=0,
            completed_after_measurement_count=0,
            censored_count=0,
            dropped_count=0,
            success_count=0,
            error_count=0,
            timeout_count=0,
            http_5xx_count=0,
            actual_start_rps=0.0,
            attainment_ratio=0.0,
            arrival_valid=False,
            error_rate=None,
            p95_latency_s=None,
            p99_latency_s=None,
        )

    def _finalize(self) -> tuple[dict[str, object], AssessmentKind]:
        stopped_at_s = (
            self.stopped_at_s
            if self.stopped_at_s is not None
            else self.warmup_s + self.duration_s
        )
        measured_s = measurement_elapsed(
            stopped_at_s=stopped_at_s,
            warmup_s=self.warmup_s,
            duration_s=self.duration_s,
        )
        with self.lock:
            samples = list(self.samples)
        summary = (
            summarize_measurement(
                samples,
                target_rps=self.rate,
                measurement_elapsed_s=measured_s,
                fixture_seconds=FIXTURE_SECONDS,
                measurement_started_at_s=self.warmup_s,
            )
            if measured_s > 0
            else self._empty_summary()
        )
        observed_arrivals = arrival_window(
            samples,
            target_rps=self.rate,
            measured_s=measured_s,
            stopped_at_s=stopped_at_s,
        )
        assessment = classify_measurement(summary, stop_reason=self.stop_reason)
        if (
            measured_s == 0.0
            and self.stop_reason in _WARMUP_BOUNDARY_REASONS
            and observed_arrivals["valid"] is True
        ):
            assessment = PhaseAssessment(
                target_rps=assessment.target_rps,
                kind=assessment.kind,
                reason=assessment.reason,
                boundary_eligible=True,
            )
        report: dict[str, object] = {
            "rate": self.rate,
            "phase_anchor_utc": self.phase_anchor_utc,
            "measurement_started_at_s": self.warmup_s,
            "measurement_started_at_utc": (
                datetime.fromisoformat(self.phase_anchor_utc)
                + timedelta(seconds=self.warmup_s)
            ).isoformat(),
            "warmup_s": self.warmup_s,
            "duration_s": self.duration_s,
            "measurement_elapsed_s": measured_s,
            "max_inflight": self.max_inflight,
            "request_deadline_s": self.request_deadline_s,
            "stop_reason": (
                self.stop_reason.value if self.stop_reason is not None else None
            ),
            "arrival_window": observed_arrivals,
            "measurement": summary.to_dict(),
            "assessment": assessment.to_dict(),
        }
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_request_jsonl(self.output / "requests.jsonl", samples)
        (self.output / "summary.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report, sort_keys=True), flush=True)
        return report, assessment.kind

    def run(self) -> int:
        if not self.mock:
            self.manifest = load_runtime_manifest(DEFAULT_MANIFEST)
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.phase_anchor_utc = datetime.now(timezone.utc).isoformat()
        self.anchor = self.clock()
        total_s = self.warmup_s + self.duration_s
        metadata = {
            "phase_anchor_utc": self.phase_anchor_utc,
            "measurement_started_at_s": self.warmup_s,
            "measurement_started_at_utc": (
                datetime.fromisoformat(self.phase_anchor_utc)
                + timedelta(seconds=self.warmup_s)
            ).isoformat(),
            "rate": self.rate,
            "warmup_s": self.warmup_s,
            "duration_s": self.duration_s,
            "max_inflight": self.max_inflight,
            "request_deadline_s": self.request_deadline_s,
            "mode": "mock" if self.mock else "api",
        }
        (self.output / "run_metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        futures: list[Future[None]] = []
        pool = ThreadPoolExecutor(
            max_workers=self.max_inflight,
            thread_name_prefix="breakpoint-request",
        )
        try:
            index = 0
            while True:
                due_s = index / self.rate
                if due_s >= total_s or not self._wait_until(due_s):
                    break
                acquired = self.slots.acquire(blocking=False)
                now_s = self._elapsed()
                reason = arrival_failure(due_s, now_s, acquired)
                if reason is not None:
                    if acquired:
                        self.slots.release()
                    self._record_dropped(
                        due_s,
                        error=(
                            RequestError.DROPPED_LATE
                            if now_s - due_s > ARRIVAL_LATE_SECONDS
                            else RequestError.SLOTS_EXHAUSTED
                        ),
                    )
                    self.abort(reason)
                    break
                futures.append(pool.submit(self._request, due_s))
                index += 1
            if self.stop_reason is None:
                self.stopped_at_s = total_s
            pending = set(futures)
            drain_deadline = time.monotonic() + DRAIN_SECONDS
            while pending and time.monotonic() < drain_deadline:
                self._print_status_if_due()
                remaining = drain_deadline - time.monotonic()
                _done, pending = wait(pending, timeout=min(0.25, remaining))
            if pending:
                if self.stop_reason is None:
                    self.abort(StopReason.TIME_LIMIT)
                self._censor_inflight()
        finally:
            pool.shutdown(wait=False, cancel_futures=False)
        _report, kind = self._finalize()
        return 0 if kind is AssessmentKind.PASS else 1


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        return BreakpointRun(
            args.output,
            rate=args.rate,
            warmup_s=args.warmup,
            duration_s=args.duration,
            mock=args.mock,
            stop_file=args.stop_file,
        ).run()
    except Exception as error:
        print("breakpoint runner failed: " + type(error).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
