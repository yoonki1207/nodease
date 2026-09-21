"""Connect the existing local Grafana/Prometheus/Loki to Docker Nodease.

Run with the repository Gateway venv (PyYAML). Preserves existing Prometheus jobs
and Grafana data. Backups are private and kept under local/monitoring-backups.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import subprocess

import yaml


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def docker(*args, check=True):
    result = subprocess.run(["docker", *map(str, args)], capture_output=True, check=False)
    if check and result.returncode:
        # Docker errors may include environment data: never echo raw output.
        raise RuntimeError(f"Docker operation failed: {args[0]}")
    return result


def install():
    for name in ("grafana", "prometheus", "loki", "moduly-nginx", "moduly-gateway", "moduly-redis"):
        state = docker("inspect", name, "--format", "{{.State.Running}}").stdout.strip()
        if state != b"true":
            raise RuntimeError(f"Required container is stopped: {name}")
    backup = ROOT / "local/monitoring-backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup.mkdir(parents=True, mode=0o700)
    docker("cp", "prometheus:/etc/prometheus/prometheus.yml", backup / "prometheus.yml")
    docker("cp", "moduly-nginx:/etc/nginx/conf.d/default.conf", backup / "nginx.conf")
    for container, remote, filename in (
        ("grafana", "/etc/grafana/provisioning/datasources/nodease.yml", "datasources.yml"),
        ("grafana", "/etc/grafana/provisioning/dashboards/nodease.yml", "dashboards.yml"),
        ("grafana", "/var/lib/grafana/nodease-dashboards/nodease.json", "dashboard.json"),
    ):
        docker("cp", f"{container}:{remote}", backup / filename, check=False)
    current = yaml.safe_load((backup / "prometheus.yml").read_text())
    jobs = current.setdefault("scrape_configs", [])
    jobs[:] = [job for job in jobs if job.get("job_name") != "nodease"]
    jobs.append({"job_name": "nodease", "scrape_interval": "15s", "scrape_timeout": "5s",
                 "static_configs": [{"targets": ["nodease-monitoring-collector:9108"],
                                     "labels": {"app": "nodease", "environment": "local"}}]})
    merged = backup / "prometheus-updated.yml"
    merged.write_text(yaml.safe_dump(current, sort_keys=False))
    if docker("network", "inspect", "nodease-monitoring", check=False).returncode:
        docker("network", "create", "--internal", "nodease-monitoring")
    for name in ("grafana", "prometheus", "loki"):
        networks = json.loads(docker("inspect", name, "--format", "{{json .NetworkSettings.Networks}}").stdout)
        if "nodease-monitoring" not in networks:
            docker("network", "connect", "nodease-monitoring", name)

    docker("cp", ROOT / "docker/nginx/nginx.conf", "moduly-nginx:/etc/nginx/conf.d/default.conf")
    if docker("exec", "moduly-nginx", "nginx", "-t", check=False).returncode:
        docker("cp", backup / "nginx.conf", "moduly-nginx:/etc/nginx/conf.d/default.conf")
        raise RuntimeError("Nginx validation failed; previous configuration restored")
    docker("exec", "moduly-nginx", "nginx", "-s", "reload")
    docker("compose", "-p", "nodease-monitoring", "-f", HERE / "compose.yml", "up", "-d")
    docker("cp", merged, "prometheus:/etc/prometheus/nodease-candidate.yml")
    docker("exec", "prometheus", "promtool", "check", "config", "/etc/prometheus/nodease-candidate.yml")
    docker("cp", merged, "prometheus:/etc/prometheus/prometheus.yml")
    docker("kill", "--signal=HUP", "prometheus")
    docker("exec", "--user", "0", "grafana", "mkdir", "-p", "/var/lib/grafana/nodease-dashboards")
    docker("cp", HERE / "datasources.yml", "grafana:/etc/grafana/provisioning/datasources/nodease.yml")
    docker("cp", HERE / "dashboards.yml", "grafana:/etc/grafana/provisioning/dashboards/nodease.yml")
    docker("cp", HERE / "dashboard.json", "grafana:/var/lib/grafana/nodease-dashboards/nodease.json")
    docker("exec", "--user", "0", "grafana", "chmod", "755", "/var/lib/grafana/nodease-dashboards")
    docker("exec", "--user", "0", "grafana", "chmod", "644",
           "/etc/grafana/provisioning/datasources/nodease.yml",
           "/etc/grafana/provisioning/dashboards/nodease.yml",
           "/var/lib/grafana/nodease-dashboards/nodease.json")
    docker("restart", "grafana")
    print(f"Installed. Configuration backup: {backup}")
    print("Dashboard: http://localhost:3000/d/nodease-local/nodease-local-operations")


if __name__ == "__main__":
    install()
