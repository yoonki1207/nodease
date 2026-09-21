#!/usr/bin/env python3
"""Record the local nodease-loadtest Redis workflow queue length."""

from __future__ import annotations

import argparse
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LOAD_DIR = Path(__file__).resolve().parent
PROJECT_NAME = "nodease-loadtest"
ENV_FILE = LOAD_DIR / ".env.load.local"
COMPOSE_FILE = REPOSITORY_ROOT / "docker" / "docker-compose.yml"
COMPOSE_OVERRIDE_FILE = LOAD_DIR / "docker-compose.override.yml"


def _compose_prefix() -> tuple[str, ...]:
    return (
        "docker",
        "--context",
        "default",
        "compose",
        "--env-file",
        str(ENV_FILE),
        "--project-name",
        PROJECT_NAME,
        "--project-directory",
        str(COMPOSE_FILE.parent),
        "--file",
        str(COMPOSE_FILE),
        "--file",
        str(COMPOSE_OVERRIDE_FILE),
    )


def queue_length() -> int | None:
    completed = subprocess.run(
        _compose_prefix()
        + ("exec", "--no-tty", "redis", "redis-cli", "LLEN", "workflow"),
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        return None
    try:
        return int(completed.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return None


def workflow_worker_count() -> int | None:
    completed = subprocess.run(
        _compose_prefix() + ("ps", "--status", "running", "--services"),
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        return None
    return sum(
        1
        for service in completed.stdout.splitlines()
        if service.strip() == "workflow_engine"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor the isolated local workflow queue"
    )
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.interval <= 0:
        raise SystemExit("--interval must be positive")
    output = args.output or (
        LOAD_DIR
        / "reports"
        / f"queue_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("timestamp,workflow_queue,workflow_workers\n", encoding="utf-8")
    print(f"recording queue metrics to {output}")
    try:
        while True:
            now = datetime.now(timezone.utc).isoformat()
            queue = queue_length()
            workers = workflow_worker_count()
            with output.open("a", encoding="utf-8") as stream:
                stream.write(
                    f"{now},{'' if queue is None else queue},"
                    f"{'' if workers is None else workers}\n"
                )
            print(
                f"workflow_queue={queue if queue is not None else 'unavailable'} "
                f"workflow_workers={workers if workers is not None else 'unavailable'}"
            )
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
