from __future__ import annotations

import builtins
import importlib.util
import json

import pytest
import requests

from tests.load.breakpoint_contract import Phase, RequestError, RequestSample, StopReason
from tests.load import run_breakpoint


def test_breakpoint_import_and_session_do_not_require_spike_runner(monkeypatch) -> None:
    original_import = builtins.__import__

    def reject_spike_import(name, *args, **kwargs):
        if name == "tests.load.run_spike":
            raise ModuleNotFoundError("run_spike is excluded")
        return original_import(name, *args, **kwargs)

    spec = importlib.util.spec_from_file_location(
        "isolated_breakpoint_runner", run_breakpoint.__file__
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", reject_spike_import)
        spec.loader.exec_module(module)

    client = module.session()
    try:
        assert isinstance(client, requests.Session)
        assert client.trust_env is False
    finally:
        client.close()


def _sample(*, phase: Phase = Phase.MEASURE) -> RequestSample:
    return RequestSample(
        scheduled_at_s=1.0,
        started_at_s=1.0,
        ended_at_s=2.0,
        phase=phase,
        status_code=200,
        error=None,
        latency_s=1.0,
        run_hash="a" * 64,
    )


def test_arrival_guard_aborts_instead_of_catching_up() -> None:
    assert run_breakpoint.arrival_failure(10.0, 10.25, True) is None
    assert (
        run_breakpoint.arrival_failure(10.0, 10.250001, True)
        is StopReason.LOADGEN_LIMIT
    )
    assert (
        run_breakpoint.arrival_failure(10.0, 10.0, False)
        is StopReason.LOADGEN_LIMIT
    )


def test_phase_uses_actual_start_and_abort_freezes_measurement_elapsed() -> None:
    assert run_breakpoint.phase_for_start(9.999, warmup_s=10.0) is Phase.WARMUP
    assert run_breakpoint.phase_for_start(10.0, warmup_s=10.0) is Phase.MEASURE
    assert run_breakpoint.measurement_elapsed(
        stopped_at_s=15.5,
        warmup_s=10.0,
        duration_s=30.0,
    ) == 5.5
    assert run_breakpoint.measurement_elapsed(
        stopped_at_s=50.0,
        warmup_s=10.0,
        duration_s=30.0,
    ) == 30.0


def test_worker_start_lateness_is_dropped_before_network_io(tmp_path) -> None:
    runner = run_breakpoint.BreakpointRun(
        tmp_path,
        rate=1.0,
        warmup_s=0.5,
        duration_s=1.0,
        mock=True,
        clock=lambda: 1.0,
    )
    assert runner.slots.acquire(blocking=False)

    runner._request(0.7)

    assert runner.stop_reason is StopReason.LOADGEN_LIMIT
    assert len(runner.samples) == 1
    assert runner.samples[0].started_at_s is None
    assert runner.samples[0].phase is Phase.MEASURE


def test_unexpected_request_exception_is_recorded_and_releases_slot(
    tmp_path,
) -> None:
    class BrokenClient:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("must not escape")

    ticks = iter((1.0, 2.0, 2.0))
    runner = run_breakpoint.BreakpointRun(
        tmp_path,
        rate=1.0,
        warmup_s=10.0,
        duration_s=1.0,
        mock=True,
        clock=lambda: next(ticks),
    )
    runner.clients.client = BrokenClient()
    assert runner.slots.acquire(blocking=False)

    runner._request(1.0)

    assert runner.samples[0].error is RequestError.OTHER
    assert runner.slots.acquire(blocking=False)
    runner.slots.release()


def test_request_jsonl_contains_only_request_sample_fields(tmp_path) -> None:
    path = tmp_path / "requests.jsonl"
    run_breakpoint.write_request_jsonl(path, [_sample()])

    row = json.loads(path.read_text(encoding="utf-8"))
    assert row == _sample().to_dict()
    assert not ({"url", "headers", "payload", "response_body"} & row.keys())


def test_external_stop_file_accepts_only_contract_stop_reasons(tmp_path) -> None:
    path = tmp_path / "stop"
    assert run_breakpoint.read_stop_reason(path) is None
    path.write_text(StopReason.HOST_RESOURCE_LIMIT.value, encoding="utf-8")
    assert run_breakpoint.read_stop_reason(path) is StopReason.HOST_RESOURCE_LIMIT
    path.write_text("unknown-or-secret-data", encoding="utf-8")
    assert run_breakpoint.read_stop_reason(path) is StopReason.OBSERVATION_LOST


@pytest.mark.parametrize(
    "argv",
    (
        ["--output", "out", "--rate", "0", "--warmup", "1", "--duration", "1"],
        ["--output", "out", "--rate", "1", "--warmup", "-1", "--duration", "1"],
        ["--output", "out", "--rate", "1", "--warmup", "1", "--duration", "0"],
    ),
)
def test_cli_rejects_non_positive_phase_inputs(argv) -> None:
    with pytest.raises(SystemExit):
        run_breakpoint.parse_args(argv)


def test_response_conversion_hashes_run_id_and_detects_marker_mismatch() -> None:
    marker = "bp-safe-marker"
    success = run_breakpoint.api_response_result(
        {
            "status": "success",
            "run_id": "run-1",
            "results": {"answer_text": f"Nodease load response: {marker}"},
        },
        marker,
    )
    assert success == (
        None,
        "4e65d3fbe8ad6535681b021b30785b12b6c0e3f8878859a4148b3f58b8835db0",
        True,
    )

    error, run_hash, marker_matches = run_breakpoint.api_response_result(
        {
            "status": "success",
            "run_id": "run-2",
            "results": {"answer_text": "wrong"},
        },
        marker,
    )
    assert error is RequestError.RESULT_MISMATCH
    assert run_hash is None
    assert marker_matches is False


def _warmup_samples(count: int) -> list[RequestSample]:
    return [
        RequestSample(
            scheduled_at_s=index / 10,
            started_at_s=index / 10,
            ended_at_s=index / 10 + 0.01,
            phase=Phase.WARMUP,
            status_code=200,
            error=None,
            latency_s=0.01,
            run_hash=f"{index:064x}",
        )
        for index in range(count)
    ]


@pytest.mark.parametrize(
    ("reason", "expected_eligible"),
    (
        (StopReason.SEVERE_RECENT_ERRORS, True),
        (StopReason.QUEUE_WAIT_EXCEEDED, True),
        (StopReason.HOST_RESOURCE_LIMIT, False),
        (StopReason.LOADGEN_LIMIT, False),
        (StopReason.OBSERVATION_LOST, False),
    ),
)
def test_valid_warmup_abort_preserves_zero_measurement_and_safe_boundary_status(
    tmp_path,
    reason: StopReason,
    expected_eligible: bool,
) -> None:
    runner = run_breakpoint.BreakpointRun(
        tmp_path / reason.value,
        rate=10.0,
        warmup_s=60.0,
        duration_s=180.0,
        mock=False,
    )
    runner.phase_anchor_utc = "2026-09-22T00:00:00+00:00"
    runner.samples = _warmup_samples(10)
    runner.stop_reason = reason
    runner.stopped_at_s = 1.0

    report, kind = runner._finalize()

    assert report["measurement_elapsed_s"] == 0.0
    assert report["measurement"]["finalized_count"] == 0
    assert report["arrival_window"] == {
        "scope": "warmup_if_aborted_before_measurement",
        "elapsed_s": 1.0,
        "scheduled_count": 10,
        "started_count": 10,
        "dropped_count": 0,
        "actual_start_rps": 10.0,
        "attainment_ratio": 1.0,
        "valid": True,
    }
    assert report["assessment"]["boundary_eligible"] is expected_eligible
    assert kind.value != "pass"


def test_warmup_abort_with_dropped_or_out_of_range_arrivals_is_ineligible(
    tmp_path,
) -> None:
    runner = run_breakpoint.BreakpointRun(
        tmp_path,
        rate=10.0,
        warmup_s=60.0,
        duration_s=180.0,
        mock=False,
    )
    runner.phase_anchor_utc = "2026-09-22T00:00:00+00:00"
    runner.samples = _warmup_samples(9) + [
        RequestSample(
            scheduled_at_s=0.9,
            started_at_s=None,
            ended_at_s=None,
            phase=Phase.WARMUP,
            status_code=None,
            error=RequestError.DROPPED_LATE,
            latency_s=None,
            run_hash=None,
        )
    ]
    runner.stop_reason = StopReason.SERVICE_CRASH
    runner.stopped_at_s = 1.0

    report, _kind = runner._finalize()

    assert report["arrival_window"]["valid"] is False
    assert report["assessment"]["boundary_eligible"] is False

    out_of_range = run_breakpoint.BreakpointRun(
        tmp_path / "out-of-range",
        rate=10.0,
        warmup_s=60.0,
        duration_s=180.0,
        mock=False,
    )
    out_of_range.phase_anchor_utc = "2026-09-22T00:00:00+00:00"
    out_of_range.samples = _warmup_samples(5)
    out_of_range.stop_reason = StopReason.SERVICE_CRASH
    out_of_range.stopped_at_s = 1.0

    out_of_range_report, _kind = out_of_range._finalize()

    assert out_of_range_report["arrival_window"]["dropped_count"] == 0
    assert out_of_range_report["arrival_window"]["attainment_ratio"] == 0.5
    assert out_of_range_report["arrival_window"]["valid"] is False
    assert out_of_range_report["assessment"]["boundary_eligible"] is False


def test_measurement_phase_keeps_existing_assessment_path(tmp_path) -> None:
    runner = run_breakpoint.BreakpointRun(
        tmp_path,
        rate=1.0,
        warmup_s=1.0,
        duration_s=100.0,
        mock=False,
    )
    runner.phase_anchor_utc = "2026-09-22T00:00:00+00:00"
    runner.samples = [
        RequestSample(
            scheduled_at_s=1.0 + index,
            started_at_s=1.0 + index,
            ended_at_s=1.1 + index,
            phase=Phase.MEASURE,
            status_code=200,
            error=None,
            latency_s=0.1,
            run_hash=f"{index:064x}",
        )
        for index in range(100)
    ]
    runner.stopped_at_s = 101.0

    report, kind = runner._finalize()

    assert report["arrival_window"]["scope"] == "measurement"
    assert report["assessment"]["boundary_eligible"] is True
    assert kind.value == "pass"
