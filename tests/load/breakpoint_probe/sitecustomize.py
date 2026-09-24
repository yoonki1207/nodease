"""Opt-in, dependency-free runtime probes for breakpoint load tests.

This module is loaded by Python's site machinery.  It deliberately imports
only the standard library and remains inert unless NODEASE_BREAKPOINT_OBSERVER
is exactly ``1``.
"""

from __future__ import annotations

import hashlib
import importlib.abc
import importlib.machinery
import json
import os
import sys
import time
from types import ModuleType
from typing import Any


_TARGETS = frozenset(
    {
        "apps.shared.celery_app",
        "apps.shared.db.session",
        "apps.workflow_engine.workflow.core.workflow_engine",
    }
)
_TASKS = frozenset(
    {
        "workflow.execute",
        "workflow.execute_deployed",
        "workflow.execute_by_deployment",
    }
)
_EVENT_FIELDS = {
    "publish": frozenset({"task_name", "task_hash"}),
    "task_start": frozenset({"task_name", "task_hash"}),
    "task_end": frozenset({"task_name", "task_hash", "success"}),
    "engine_start": frozenset({"task_hash", "run_hash"}),
    "engine_end": frozenset({"duration", "success", "task_hash", "run_hash"}),
    "pool_acquire": frozenset({"duration", "success"}),
    "pool_checkout": frozenset({"duration", "success"}),
    "probe_ready": frozenset(),
}
_HEX_DIGITS = frozenset("0123456789abcdef")
_HOOKED = "__nodease_breakpoint_probe_hooked__"
_ORIGINAL = "__nodease_breakpoint_probe_original__"


def _hash(value: Any) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _task_name(value: Any) -> str | None:
    name = getattr(value, "name", value)
    return name if isinstance(name, str) and name in _TASKS else None


def _service_name() -> str:
    value = os.getenv("NODEASE_BREAKPOINT_SERVICE", "unknown")
    if not value or len(value) > 64:
        return "unknown"
    if any(not (character.isalnum() or character in "._-") for character in value):
        return "unknown"
    return value


def _safe_event_fields(event: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    allowed = _EVENT_FIELDS.get(event)
    if allowed is None:
        return None
    safe: dict[str, Any] = {}
    for key in allowed:
        value = fields.get(key)
        if key == "task_name":
            value = _task_name(value)
        elif key in {"task_hash", "run_hash"}:
            if not (
                isinstance(value, str)
                and len(value) == 64
                and all(character in _HEX_DIGITS for character in value)
            ):
                value = None
        elif key == "duration":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                value = None
            elif value < 0 or value != value or value == float("inf"):
                value = None
        elif key == "success" and not isinstance(value, bool):
            value = None
        if value is not None:
            safe[key] = value
    return safe


def _emit(event: str, **fields: Any) -> None:
    if os.getenv("NODEASE_BREAKPOINT_OBSERVER") != "1":
        return
    directory = os.getenv("NODEASE_BREAKPOINT_EVENT_DIR")
    if not directory:
        return
    safe_fields = _safe_event_fields(event, fields)
    if safe_fields is None:
        return
    payload: dict[str, Any] = {
        "event": event,
        "ts": time.time(),
        "service": _service_name(),
        "pid": os.getpid(),
    }
    payload.update(safe_fields)
    try:
        encoded = (
            json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode()
        path = os.path.join(directory, f"{payload['service']}-{payload['pid']}.jsonl")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, encoded)
        finally:
            os.close(fd)
    except OSError:
        # A probe must never change application behavior.
        return


def _task_id_from(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("id", "task_id", "uuid"):
            if value.get(key):
                return _hash(value[key])
    return None


def _install_celery(module: ModuleType) -> None:
    app = getattr(module, "celery_app", None)
    if app is None or getattr(app, _HOOKED, False):
        return
    signals = sys.modules.get("celery.signals")
    if signals is not None:

        def before_publish(sender=None, headers=None, **_kwargs: Any) -> None:
            name = _task_name(sender or (headers or {}).get("task"))
            if name:
                _emit("publish", task_name=name, task_hash=_task_id_from(headers))

        def prerun(sender=None, task_id=None, **_kwargs: Any) -> None:
            name = _task_name(sender)
            if name:
                _emit("task_start", task_name=name, task_hash=_hash(task_id))

        def postrun(sender=None, task_id=None, state=None, **_kwargs: Any) -> None:
            name = _task_name(sender)
            if name:
                _emit(
                    "task_end",
                    task_name=name,
                    task_hash=_hash(task_id),
                    success=state == "SUCCESS",
                )

        signals.before_task_publish.connect(before_publish, weak=False)
        signals.task_prerun.connect(prerun, weak=False)
        signals.task_postrun.connect(postrun, weak=False)
    setattr(app, _HOOKED, True)
    _emit("probe_ready")


def _current_task_hash() -> str | None:
    celery = sys.modules.get("celery")
    try:
        request = getattr(getattr(celery, "current_task", None), "request", None)
        return _hash(getattr(request, "id", None))
    except Exception:
        return None


def _engine_fields(engine: Any) -> dict[str, str | None]:
    run_id = getattr(getattr(engine, "logger", None), "workflow_run_id", None)
    if run_id is None:
        context = getattr(engine, "execution_context", {}) or {}
        if isinstance(context, dict):
            run_id = context.get("workflow_run_id")
    return {"task_hash": _current_task_hash(), "run_hash": _hash(run_id)}


def _install_engine(module: ModuleType) -> None:
    cls = getattr(module, "WorkflowEngine", None)
    if cls is None or getattr(cls, _HOOKED, False):
        return
    original = cls._execute_core

    def execute_core(self, *args: Any, **kwargs: Any) -> Any:
        fields = _engine_fields(self)
        started = time.monotonic()
        _emit("engine_start", **fields)
        success = False
        try:
            yield from original(self, *args, **kwargs)
            success = True
        finally:
            final_fields = _engine_fields(self)
            if final_fields["run_hash"] is None:
                final_fields["run_hash"] = fields["run_hash"]
            if final_fields["task_hash"] is None:
                final_fields["task_hash"] = fields["task_hash"]
            _emit(
                "engine_end",
                duration=time.monotonic() - started,
                success=success,
                **final_fields,
            )

    setattr(execute_core, _ORIGINAL, original)
    setattr(cls, "_execute_core", execute_core)
    setattr(cls, _HOOKED, True)


def _install_db(module: ModuleType) -> None:
    engine = getattr(module, "engine", None)
    pool = getattr(engine, "pool", None)
    if pool is None or getattr(pool, _HOOKED, False):
        return
    original_get = getattr(pool, "_do_get", None)
    original_connect = getattr(pool, "connect", None)
    if original_get is not None:

        def do_get(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            try:
                value = original_get(*args, **kwargs)
            except BaseException:
                _emit("pool_acquire", duration=time.monotonic() - started, success=False)
                raise
            _emit("pool_acquire", duration=time.monotonic() - started, success=True)
            return value

        pool._do_get = do_get
    if original_connect is not None:

        def connect(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            try:
                value = original_connect(*args, **kwargs)
            except BaseException:
                _emit("pool_checkout", duration=time.monotonic() - started, success=False)
                raise
            _emit("pool_checkout", duration=time.monotonic() - started, success=True)
            return value

        pool.connect = connect
    setattr(pool, _HOOKED, True)


def _install(module: ModuleType) -> None:
    if os.getenv("NODEASE_BREAKPOINT_OBSERVER") != "1":
        return
    if module.__name__ == "apps.shared.celery_app":
        _install_celery(module)
    elif module.__name__ == "apps.shared.db.session":
        _install_db(module)
    elif module.__name__ == "apps.workflow_engine.workflow.core.workflow_engine":
        _install_engine(module)


def _safe_install(module: ModuleType) -> None:
    try:
        _install(module)
    except Exception:
        # Observability must never stop an application module from importing.
        return


class _Loader(importlib.abc.Loader):
    def __init__(self, loader: Any):
        self.loader = loader

    def create_module(self, spec: Any) -> Any:
        creator = getattr(self.loader, "create_module", None)
        return creator(spec) if creator else None

    def exec_module(self, module: ModuleType) -> None:
        self.loader.exec_module(module)
        _safe_install(module)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname not in _TARGETS:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is not None and spec.loader is not None:
            spec.loader = _Loader(spec.loader)
        return spec


if os.getenv("NODEASE_BREAKPOINT_OBSERVER") == "1":
    sys.meta_path.insert(0, _Finder())
    for _name in _TARGETS:
        _module = sys.modules.get(_name)
        if _module is not None:
            _safe_install(_module)
