from __future__ import annotations

import importlib.util
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest


PROBE = Path(__file__).parents[1] / "load" / "breakpoint_probe" / "sitecustomize.py"


def load_probe(monkeypatch, *, enabled=False):
    if not enabled:
        monkeypatch.delenv("NODEASE_BREAKPOINT_OBSERVER", raising=False)
    else:
        monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    spec = importlib.util.spec_from_file_location("breakpoint_probe_sitecustomize", PROBE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_opt_in_disabled_does_not_modify_meta_path(monkeypatch):
    before = list(__import__("sys").meta_path)
    load_probe(monkeypatch)
    assert __import__("sys").meta_path == before


def test_opt_in_startup_does_not_import_gevent_sensitive_modules(tmp_path):
    command = [
        sys.executable,
        "-c",
        (
            "import sys; print(','.join(name for name in "
            "('socket','ssl','threading','select') if name in sys.modules))"
        ),
    ]
    baseline_environment = os.environ.copy()
    baseline_environment.pop("PYTHONPATH", None)
    baseline_environment.pop("NODEASE_BREAKPOINT_OBSERVER", None)
    baseline = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        env=baseline_environment,
    )

    probe_environment = baseline_environment.copy()
    probe_environment["PYTHONPATH"] = str(PROBE.parent)
    probe_environment["NODEASE_BREAKPOINT_OBSERVER"] = "1"
    probe_environment["NODEASE_BREAKPOINT_EVENT_DIR"] = str(tmp_path)
    with_probe = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        env=probe_environment,
    )

    assert set(filter(None, with_probe.stdout.strip().split(","))) <= set(
        filter(None, baseline.stdout.strip().split(","))
    )


def test_emit_is_redacted_and_hashes_are_stable(tmp_path, monkeypatch):
    monkeypatch.setenv("NODEASE_BREAKPOINT_OBSERVER", "1")
    monkeypatch.setenv("NODEASE_BREAKPOINT_EVENT_DIR", str(tmp_path))
    monkeypatch.setenv("NODEASE_BREAKPOINT_SERVICE", "test")
    probe = load_probe(monkeypatch, enabled=True)
    probe._emit(
        "task_start",
        task_name="workflow.execute",
        task_hash=probe._hash("secret"),
        args={"token": "raw-secret"},
        result="raw-result",
        payload="raw-payload",
        sql="select raw-secret",
        url="postgresql://raw-secret",
    )
    rows = [
        json.loads(line)
        for path in tmp_path.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    data = next(row for row in rows if row["event"] == "task_start")
    assert data["event"] == "task_start"
    assert data["task_hash"] == probe._hash("secret")
    encoded = json.dumps(data)
    assert "secret" not in encoded
    assert "raw-result" not in encoded
    assert set(data) == {"event", "pid", "service", "task_hash", "task_name", "ts"}


def test_task_filter_includes_all_supported_workflow_entrypoints(monkeypatch):
    probe = load_probe(monkeypatch)
    assert probe._task_name("workflow.execute") == "workflow.execute"
    assert probe._task_name("workflow.execute_deployed") == "workflow.execute_deployed"
    assert (
        probe._task_name("workflow.execute_by_deployment")
        == "workflow.execute_by_deployment"
    )
    assert probe._task_name("log.create_run") is None


def test_celery_signals_emit_publish_start_end_and_ready(monkeypatch):
    probe = load_probe(monkeypatch)
    events = []
    monkeypatch.setattr(
        probe,
        "_emit",
        lambda event, **fields: events.append((event, fields)),
    )

    class Signal:
        def connect(self, receiver, **_kwargs):
            self.receiver = receiver

    signals = type(
        "Signals",
        (),
        {
            "before_task_publish": Signal(),
            "task_prerun": Signal(),
            "task_postrun": Signal(),
        },
    )
    monkeypatch.setitem(sys.modules, "celery.signals", signals)
    app = type("CeleryApp", (), {})()
    probe._install_celery(type("Module", (), {"celery_app": app}))

    signals.before_task_publish.receiver(
        sender="workflow.execute_deployed",
        headers={"id": "task-1"},
    )
    sender = type("Task", (), {"name": "workflow.execute_deployed"})()
    signals.task_prerun.receiver(sender=sender, task_id="task-1")
    signals.task_postrun.receiver(sender=sender, task_id="task-1", state="SUCCESS")

    assert [event for event, _fields in events] == [
        "probe_ready",
        "publish",
        "task_start",
        "task_end",
    ]
    task_hash = probe._hash("task-1")
    assert all(
        fields.get("task_hash") == task_hash
        for event, fields in events
        if event != "probe_ready"
    )
    assert events[-1][1]["success"] is True


def test_engine_wrapper_measures_core_iteration_for_regular_and_deployed_paths(
    monkeypatch,
):
    probe = load_probe(monkeypatch)
    events = []
    monkeypatch.setattr(
        probe,
        "_emit",
        lambda event, **fields: events.append((event, fields)),
    )

    class Engine:
        def __init__(self):
            self.execution_context = {"workflow_run_id": "run-1"}
            self.logger = object()

        def _execute_core(self, stream_mode=False):
            yield {"type": "workflow_finish", "data": {"stream": stream_mode}}

        def execute(self):
            return list(self._execute_core(stream_mode=True))

        def execute_deployed(self):
            return list(self._execute_core(stream_mode=False))

    module = type("Module", (), {"WorkflowEngine": Engine})
    probe._install_engine(module)
    iterator = Engine()._execute_core(stream_mode=True)
    assert events == []
    assert list(iterator) == [{"type": "workflow_finish", "data": {"stream": True}}]
    assert Engine().execute_deployed() == [
        {"type": "workflow_finish", "data": {"stream": False}}
    ]

    event_names = [event for event, _fields in events]
    assert event_names == ["engine_start", "engine_end"] * 2
    for event, fields in events:
        assert fields["run_hash"] == probe._hash("run-1")
        if event == "engine_end":
            assert fields["success"] is True
            assert math.isfinite(fields["duration"])
            assert fields["duration"] >= 0


def test_engine_wrapper_preserves_generator_error_and_emits_terminal_event(monkeypatch):
    probe = load_probe(monkeypatch)
    events = []
    monkeypatch.setattr(
        probe,
        "_emit",
        lambda event, **fields: events.append((event, fields)),
    )

    class Engine:
        execution_context = {}

        def _execute_core(self, stream_mode=False):
            yield "started"
            raise RuntimeError("preserve")

    module = type("Module", (), {"WorkflowEngine": Engine})
    probe._install_engine(module)

    iterator = Engine()._execute_core()
    assert next(iterator) == "started"
    with pytest.raises(RuntimeError, match="preserve"):
        next(iterator)

    assert [event for event, _fields in events] == ["engine_start", "engine_end"]
    assert events[-1][1]["success"] is False


def test_engine_wrapper_hashes_current_celery_task_id(monkeypatch):
    probe = load_probe(monkeypatch)
    events = []
    celery = type(
        "CeleryModule",
        (),
        {
            "current_task": type(
                "Task",
                (),
                {"request": type("Request", (), {"id": "task-1"})()},
            )()
        },
    )
    monkeypatch.setitem(sys.modules, "celery", celery)
    monkeypatch.setattr(
        probe,
        "_emit",
        lambda event, **fields: events.append((event, fields)),
    )

    class Engine:
        execution_context = {}

        def _execute_core(self, stream_mode=False):
            yield "done"

    probe._install_engine(type("Module", (), {"WorkflowEngine": Engine}))
    assert list(Engine()._execute_core()) == ["done"]
    assert all(fields["task_hash"] == probe._hash("task-1") for _, fields in events)


def test_pool_wrappers_emit_finite_measurements(monkeypatch):
    probe = load_probe(monkeypatch)
    events = []
    monkeypatch.setattr(
        probe,
        "_emit",
        lambda event, **fields: events.append((event, fields)),
    )

    class Pool:
        def _do_get(self):
            return "connection"

        def connect(self):
            return "fairy"

    pool = Pool()
    db_module = type("Module", (), {"engine": type("Engine", (), {"pool": pool})()})
    probe._install_db(db_module)
    assert pool._do_get() == "connection"
    assert pool.connect() == "fairy"
    durations = [fields["duration"] for _event, fields in events if "duration" in fields]
    assert durations and all(math.isfinite(value) and value >= 0 for value in durations)
