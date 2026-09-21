"""Local monitoring must never export request content or stale success data."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import struct

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def collector():
    spec = importlib.util.spec_from_file_location(
        "nodease_collector", ROOT / "docker/monitoring/collector.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_access_metrics_only_accept_bounded_labels_and_finite_duration(collector):
    line = json.dumps({"nodease_access": True, "surface": "api", "method": "GET",
                       "status": 503, "duration": 0.25, "unexpected": "discard-me"})
    assert collector.parse_access(line) == ("api", "GET", "503", 0.25)
    assert collector.parse_access(line.replace('"GET"', '"discard-me"')) is None
    assert collector.parse_access(line.replace("0.25", "NaN")) is None
    assert collector.parse_access(line.replace("503", "999")) is None
    assert collector.parse_access("ordinary request log") is None


def test_error_export_discards_all_original_message_content(collector):
    assert collector.error_level("ERROR: discard-me") == "error"
    assert collector.error_level("[2026-09-05 12:00:00,123: WARNING/MainProcess] discard-me") == "warning"
    assert collector.error_level("INFO: user supplied ERROR text") is None
    event = collector.safe_event("gateway", "error", 2)
    assert event == {"service": "gateway", "level": "error", "count": 2,
                     "event": "container_log_error", "message": "Original message retained only in Docker logs"}


def test_docker_frames_do_not_merge_stdout_and_stderr_partial_lines(collector):
    def frame(stream, text):
        data = text.encode()
        return struct.pack(">BxxxI", stream, len(data)) + data
    raw = frame(1, "first") + frame(2, "error\n") + frame(1, " line\n")
    assert sorted(collector.docker_lines(raw)) == ["error", "first line"]
    with pytest.raises(ValueError):
        collector.docker_lines(raw[:-1])


def test_priority_queue_depth_includes_all_kombu_buckets(collector):
    class Redis:
        def llen(self, key):
            return {"workflow": 1, "workflow\x06\x163": 2,
                    "workflow\x06\x166": 3, "workflow\x06\x169": 4}.get(key, 0)
    assert collector.queue_depth(Redis(), "workflow") == 10


def test_histogram_cumulative_buckets_and_no_raw_labels(collector):
    metrics = collector.AccessMetrics()
    metrics.observe(("api", "GET", "503", 0.25))
    output = metrics.render()
    assert 'nodease_http_requests_total{surface="api",method="GET",status="503"} 1' in output
    assert 'nodease_http_request_duration_seconds_bucket{surface="api",le="0.1"} 0' in output
    assert 'nodease_http_request_duration_seconds_bucket{surface="api",le="0.25"} 1' in output
    assert 'nodease_http_request_duration_seconds_bucket{surface="api",le="+Inf"} 1' in output


def test_safe_nginx_log_format_omits_request_identifiers():
    source = (ROOT / "docker/nginx/nginx.conf").read_text()
    assert "log_format nodease_metrics" in source
    log_format = source.split("log_format nodease_metrics", 1)[1].split(";", 1)[0]
    assert "$request_time" in log_format
    for sensitive in ("$request_uri", "$request ", "$args", "$remote_addr", "$http_", "$request_body"):
        assert sensitive not in log_format
    # Keep the explicitly protected ingress routes unlogged.
    for location in ("location /api/v1/hooks/", "location ~ ^/api/v1/connectors/test/?$"):
        block = source.split(location, 1)[1].split("}", 1)[0]
        assert "access_log off;" in block


def test_poll_omits_queue_values_when_redis_fails(collector, monkeypatch):
    monkeypatch.setattr(collector, "docker_get", lambda _path: [])

    class BrokenRedis:
        def llen(self, _key):
            raise OSError("discard-me")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    monkeypatch.setattr(collector, "urlopen", lambda *_args, **_kwargs: Response())
    instance = collector.Collector()
    instance.redis = BrokenRedis()
    instance.poll()
    assert "nodease_redis_collection_success 0" in instance.snapshot
    assert "nodease_queue_depth" not in instance.snapshot
    assert 'nodease_container_up{service="gateway"} 0' in instance.snapshot
    assert "discard-me" not in instance.snapshot


def test_inclusive_docker_log_boundary_deduplicates_without_losing_equal_lines(collector, monkeypatch):
    message = '2026-09-05T10:00:00.000000000Z ERROR: discard-me\n'
    data = message.encode() * 2
    raw = struct.pack(">BxxxI", 2, len(data)) + data
    monkeypatch.setattr(collector, "docker_get", lambda *_args, **_kwargs: raw)
    end = 1788602400
    instance = collector.Collector()
    container = {"Id": "example", "State": "exited"}
    first = instance.read_container("gateway", container, end)
    assert first[4] == ["ERROR: discard-me", "ERROR: discard-me"]
    instance.cursors["example"] = first[5]
    second = instance.read_container("gateway", container, end + 1)
    assert second[4] == []


def test_failed_log_read_preserves_cursor_for_retry(collector, monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("discard-me")

    monkeypatch.setattr(collector, "docker_get", fail)
    instance = collector.Collector()
    instance.cursors["example"] = (123, collector.Counter())
    result = instance.read_container("gateway", {"Id": "example", "State": "exited"}, 150)
    assert result[4] is None
    assert instance.cursors["example"][0] == 123


def test_loki_failure_retains_only_sanitized_events_for_retry(collector, monkeypatch):
    monkeypatch.setattr(collector, "docker_get", lambda _path: [])

    class Redis:
        def llen(self, _key):
            return 0

    def fail(*_args, **_kwargs):
        raise OSError("discard-me")

    monkeypatch.setattr(collector, "urlopen", fail)
    instance = collector.Collector()
    instance.redis = Redis()
    instance.pending = [{"stream": {"job": "nodease", "service": "gateway", "level": "error"},
                         "values": [["1", json.dumps(collector.safe_event("gateway", "error", 1))]]}]
    instance.poll()
    assert len(instance.pending) == 1
    assert "nodease_loki_delivery_success 0" in instance.snapshot
    assert "discard-me" not in json.dumps(instance.pending)


def test_failed_collection_does_not_keep_stale_healthy_snapshot(collector, monkeypatch):
    instance = collector.Collector()
    instance.snapshot = 'nodease_container_up{service="gateway"} 1\n'

    def fail():
        raise OSError("discard-me")

    def stop(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(instance, "poll", fail)
    monkeypatch.setattr(collector.time, "sleep", stop)
    with pytest.raises(KeyboardInterrupt):
        instance.run()
    assert instance.snapshot == "nodease_collection_success 0\n"
