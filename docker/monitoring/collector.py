"""Local Docker-only exporter. Exports allowlisted metadata, never log contents.

Docker access is GET-only in this program, but a socket mount itself grants daemon
authority: this collector is for the trusted personal Docker environment only.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
import re
import socket
import struct
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SERVICES = ("postgres", "redis", "gateway", "knowledge-worker", "workflow-engine",
            "log-system", "log-system-beat", "frontend", "sandbox", "nginx", "proxy")
QUEUES = ("workflow", "log", "knowledge", "celery")
BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0)
SURFACES = ("api", "frontend", "websocket")
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def parse_access(line):
    try:
        data = json.loads(line)
        if not isinstance(data, dict) or data.get("nodease_access") is not True:
            return None
        surface, method, status = data["surface"], data["method"], data["status"]
        duration = float(data["duration"])
        if (surface not in SURFACES or method not in METHODS
                or type(status) is not int or not 100 <= status <= 599
                or not math.isfinite(duration) or duration < 0):
            return None
        return surface, method, str(status), duration
    except (ValueError, TypeError, KeyError):
        return None


def error_level(line):
    # Only recognized logger prefixes, never arbitrary occurrences in messages.
    match = re.match(r"^(ERROR|WARNING|CRITICAL|FATAL)(?:[:\[])|"
                     r"^\[[0-9 ,:.\-]+: (ERROR|WARNING|CRITICAL|FATAL)/|"
                     r"^\d{4}/\d\d/\d\d [0-9:]+ \[(error|warn|crit|alert|emerg)\]", line)
    if not match:
        return None
    value = next(group for group in match.groups() if group).lower()
    return "warning" if value in ("warning", "warn") else "error"


def safe_event(service, level, count):
    if service not in SERVICES or level not in ("error", "warning"):
        raise ValueError("Invalid metadata")
    return {"service": service, "level": level, "count": int(count),
            "event": "container_log_error",
            "message": "Original message retained only in Docker logs"}


def docker_lines(raw):
    streams = defaultdict(bytearray)
    offset = 0
    while offset < len(raw):
        if len(raw) - offset < 8:
            raise ValueError("Incomplete Docker frame")
        stream, length = struct.unpack(">BxxxI", raw[offset:offset + 8])
        if stream not in (1, 2) or len(raw) - offset - 8 < length:
            raise ValueError("Invalid Docker frame")
        streams[stream].extend(raw[offset + 8:offset + 8 + length])
        offset += 8 + length
    return [line for data in streams.values()
            for line in data.decode("utf-8", errors="replace").splitlines()]


def queue_depth(client, queue):
    return sum(client.llen(queue if priority == 0 else f"{queue}\x06\x16{priority}")
               for priority in (0, 3, 6, 9))


class AccessMetrics:
    def __init__(self):
        self.requests = Counter()
        self.buckets = Counter()
        self.counts = Counter()
        self.sums = Counter()

    def observe(self, observation):
        surface, method, status, duration = observation
        self.requests[surface, method, status] += 1
        self.counts[surface] += 1
        self.sums[surface] += duration
        for bound in BUCKETS:
            if duration <= bound:
                self.buckets[surface, bound] += 1

    def render(self):
        lines = ["# TYPE nodease_http_requests_total counter",
                 "# TYPE nodease_http_request_duration_seconds histogram"]
        for (surface, method, status), count in sorted(self.requests.items()):
            lines.append(f'nodease_http_requests_total{{surface="{surface}",method="{method}",status="{status}"}} {count}')
        for surface in SURFACES:
            for bound in BUCKETS:
                lines.append(f'nodease_http_request_duration_seconds_bucket{{surface="{surface}",le="{bound:g}"}} {self.buckets[surface, bound]}')
            lines.extend([
                f'nodease_http_request_duration_seconds_bucket{{surface="{surface}",le="+Inf"}} {self.counts[surface]}',
                f'nodease_http_request_duration_seconds_count{{surface="{surface}"}} {self.counts[surface]}',
                f'nodease_http_request_duration_seconds_sum{{surface="{surface}"}} {self.sums[surface]}',
            ])
        return "\n".join(lines)


class DockerConnection(HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=5)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")


def docker_get(path, *, binary=False):
    connection = DockerConnection()
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError("Docker read failed")
        raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise RuntimeError("Docker response exceeds collector limit")
        return raw if binary else json.loads(raw)
    finally:
        connection.close()


class Collector:
    def __init__(self):
        self.started = int(time.time())
        self.access = AccessMetrics()
        self.cursors = {}
        self.errors = Counter()
        self.dropped = 0
        self.pending = []
        self.lock = threading.Lock()
        self.snapshot = "nodease_collection_success 0\n"
        self.redis = None

    def read_container(self, service, container, end):
        identifier = container["Id"]
        stats = None
        stats_ok = True
        if container["State"] == "running":
            try:
                stats = docker_get(f"/containers/{identifier}/stats?stream=false&one-shot=true")
            except Exception:
                stats_ok = False
        since, previous = self.cursors.get(identifier, (self.started, Counter()))
        query = urlencode({"stdout": 1, "stderr": 1, "timestamps": 1, "since": since, "until": end})
        try:
            lines = docker_lines(docker_get(f"/containers/{identifier}/logs?{query}", binary=True))
        except Exception:
            return service, container, stats, stats_ok, None, None
        # Docker's timestamp boundaries are inclusive. A multiset also preserves
        # identical messages emitted at the same timestamp, without recounting them.
        boundary = Counter()
        filtered = []
        for line in lines:
            timestamp, _, message = line.partition(" ")
            digest = hashlib.sha256(line.encode()).digest()
            if previous[digest]:
                previous[digest] -= 1
            else:
                filtered.append(message)
            # Keep only lines at the ending second for the next inclusive poll.
            if timestamp[:19] == time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(end)):
                boundary[digest] += 1
        return service, container, stats, stats_ok, filtered, (end, boundary)

    def poll(self):
        end = int(time.time()) - 1
        containers = docker_get("/containers/json?all=true")
        by_name = {name.lstrip("/"): item for item in containers for name in item["Names"]}
        lines = []
        work = []
        for service in SERVICES:
            container = by_name.get("moduly-" + service)
            running = container is not None and container["State"] == "running"
            lines.append(f'nodease_container_up{{service="{service}"}} {int(running)}')
            if container is not None:
                work.append((service, container, end))
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda args: self.read_container(*args), work))
        active_ids = {item[1]["Id"] for item in work}
        self.cursors = {key: value for key, value in self.cursors.items() if key in active_ids}
        for service, container, stats, stats_ok, messages, cursor in results:
            label = f'{{service="{service}"}}'
            lines.append(f"nodease_container_stats_success{label} {int(stats_ok)}")
            lines.append(f"nodease_container_logs_success{label} {int(messages is not None)}")
            if stats:
                cpu = stats.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0) / 1e9
                memory = stats.get("memory_stats", {})
                cache = memory.get("stats", {}).get("inactive_file", memory.get("stats", {}).get("total_inactive_file", 0))
                used = max(0, memory.get("usage", 0) - cache)
                lines.extend([f"nodease_container_cpu_seconds_total{label} {cpu}",
                              f"nodease_container_memory_bytes{label} {used}",
                              f'nodease_container_memory_limit_bytes{label} {memory.get("limit", 0)}'])
            if messages is not None:
                self.cursors[container["Id"]] = cursor
                levels = Counter()
                for message in messages:
                    observation = parse_access(message) if service == "nginx" else None
                    if observation:
                        self.access.observe(observation)
                    level = error_level(message)
                    if level:
                        levels[level] += 1
                        self.errors[service, level] += 1
                for level, count in levels.items():
                    self.pending.append({
                        "stream": {"job": "nodease", "service": service, "level": level},
                        "values": [[str(time.time_ns()), json.dumps(safe_event(service, level, count))]],
                    })
        try:
            if self.redis is None:
                import redis
                self.redis = redis.Redis(host="moduly-redis", port=6379, db=0,
                                         socket_timeout=2, socket_connect_timeout=2)
            # Publish the group atomically; a failed read is never displayed as zero.
            queue_lines = [f'nodease_queue_depth{{queue="{queue}"}} {queue_depth(self.redis, queue)}' for queue in QUEUES]
            lines.extend(queue_lines)
            lines.append("nodease_redis_collection_success 1")
        except Exception:
            lines.append("nodease_redis_collection_success 0")
        if len(self.pending) > 1000:
            self.dropped += len(self.pending) - 1000
            self.pending = self.pending[-1000:]
        loki_ok = True
        try:
            url = os.getenv("LOKI_URL", "http://loki:3100")
            if self.pending:
                request = Request(url + "/loki/api/v1/push", json.dumps({"streams": self.pending}).encode(),
                                  {"Content-Type": "application/json"}, method="POST")
            else:
                request = Request(url + "/ready")
            with urlopen(request, timeout=5) as response:
                if response.status not in (200, 204):
                    raise RuntimeError("Loki request failed")
            self.pending.clear()
        except Exception:
            loki_ok = False
        lines.extend([self.access.render(), f"nodease_loki_delivery_success {int(loki_ok)}",
                      f"nodease_log_batches_dropped_total {self.dropped}",
                      "nodease_collection_success 1", f"nodease_collection_timestamp_seconds {time.time()}"])
        for service in SERVICES:
            for level in ("warning", "error"):
                lines.append(f'nodease_log_events_total{{service="{service}",level="{level}"}} {self.errors[service, level]}')
        with self.lock:
            self.snapshot = "\n".join(lines) + "\n"

    def run(self):
        while True:
            started = time.monotonic()
            try:
                self.poll()
            except Exception:
                # A failed collection must not serve stale healthy values.
                with self.lock:
                    self.snapshot = "nodease_collection_success 0\n"
                print("Nodease collection failed; details omitted", flush=True)
            time.sleep(max(0.1, 10 - (time.monotonic() - started)))


def main():
    collector = Collector()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            with collector.lock:
                body = collector.snapshot.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    threading.Thread(target=collector.run, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 9108), Handler).serve_forever()


if __name__ == "__main__":
    main()
