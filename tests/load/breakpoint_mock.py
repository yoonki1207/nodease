"""Dependency-free delayed HTTP mock for breakpoint load tests."""

from __future__ import annotations

import argparse
import json
import math
import re
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit


MIN_DELAY_SECONDS = 0.0
MAX_DELAY_SECONDS = 30.0
DEFAULT_MAX_CONCURRENCY = 512
MAX_CONFIGURED_CONCURRENCY = 4096
_MARKER_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class DelayBoundsError(ValueError):
    pass


class MarkerError(ValueError):
    pass


def parse_delay_seconds(raw_value: str | None) -> float:
    try:
        value = float(raw_value) if raw_value not in {None, ""} else math.nan
    except (TypeError, ValueError) as exc:
        raise DelayBoundsError("delay_out_of_range") from exc
    if not math.isfinite(value) or not MIN_DELAY_SECONDS <= value <= MAX_DELAY_SECONDS:
        raise DelayBoundsError("delay_out_of_range")
    return value


def validate_marker(raw_value: str | None) -> str:
    value = str(raw_value or "")
    if not _MARKER_PATTERN.fullmatch(value):
        raise MarkerError("marker_invalid")
    return value


class MockState:
    """Thread-safe aggregate counters with no request or marker retention."""

    def __init__(
        self,
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 1 <= max_concurrency <= MAX_CONFIGURED_CONCURRENCY:
            raise ValueError("max_concurrency is out of range")
        self.max_concurrency = max_concurrency
        self.sleep = sleep
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self._lock = threading.Lock()
        self._active_requests = 0
        self._max_active_requests = 0
        self._request_count = 0
        self._success_count = 0
        self._error_count = 0
        self._rejected_count = 0

    def try_begin(self) -> bool:
        with self._lock:
            self._request_count += 1
        if not self._slots.acquire(blocking=False):
            with self._lock:
                self._rejected_count += 1
            return False
        with self._lock:
            self._active_requests += 1
            self._max_active_requests = max(
                self._max_active_requests,
                self._active_requests,
            )
        return True

    def finish_success(self) -> None:
        with self._lock:
            self._active_requests -= 1
            self._success_count += 1
        self._slots.release()

    def finish_error(self) -> None:
        with self._lock:
            self._active_requests -= 1
            self._error_count += 1
        self._slots.release()

    def record_invalid_request(self) -> None:
        with self._lock:
            self._request_count += 1
            self._error_count += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "active_requests": self._active_requests,
                "max_active_requests": self._max_active_requests,
                "request_count": self._request_count,
                "success_count": self._success_count,
                "error_count": self._error_count,
                "rejected_count": self._rejected_count,
                "configured_max_concurrency": self.max_concurrency,
            }


class BreakpointMockServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 1024


class BreakpointRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mock_state: MockState

    def log_message(self, _format: str, *args: object) -> None:
        # Request targets contain markers. Do not put them in logs.
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if parsed.path == "/metrics":
            self._send_json(HTTPStatus.OK, self.mock_state.snapshot())
            return
        if parsed.path != "/delay":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return

        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=False)
        try:
            if len(query.get("seconds", ())) != 1 or len(query.get("marker", ())) != 1:
                raise ValueError("query_invalid")
            delay_seconds = parse_delay_seconds(query["seconds"][0])
            marker = validate_marker(query["marker"][0])
        except (DelayBoundsError, MarkerError, ValueError):
            self.mock_state.record_invalid_request()
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "request_invalid"})
            return

        if not self.mock_state.try_begin():
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "mock_saturated"})
            return

        try:
            self.mock_state.sleep(delay_seconds)
            self._send_json(
                HTTPStatus.OK,
                {
                    "marker": marker,
                    "answer_text": f"Nodease load response: {marker}",
                    "delay_seconds": delay_seconds,
                },
            )
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            self.mock_state.finish_error()
            return
        except Exception:
            self.mock_state.finish_error()
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "mock_internal_error"},
            )
            return
        self.mock_state.finish_success()

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        content = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(content)


def create_mock_server(
    *,
    host: str,
    port: int,
    state: MockState | None = None,
) -> BreakpointMockServer:
    selected_state = state or MockState()

    class BoundBreakpointRequestHandler(BreakpointRequestHandler):
        mock_state = selected_state

    return BreakpointMockServer((host, port), BoundBreakpointRequestHandler)


@contextmanager
def running_mock_server(
    *,
    state: MockState | None = None,
) -> Iterator[BreakpointMockServer]:
    server = create_mock_server(host="127.0.0.1", port=0, state=state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the breakpoint delay mock")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=DEFAULT_MAX_CONCURRENCY,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    state = MockState(max_concurrency=args.max_concurrency)
    server = create_mock_server(host=args.host, port=args.port, state=state)
    print(f"breakpoint mock listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
