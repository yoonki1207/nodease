# Nodease local Grafana connection

Connects the existing `grafana`, `prometheus`, and `loki` containers to the
`docker/docker-compose.yml` Nodease stack. This is a personal local monitoring
configuration, not a multi-tenant monitoring service.

## Contents and coverage

- `dashboard.json`: Nodease Local Operations dashboard (`nodease-local` UID).
- `collector.py`: Docker container CPU, cache-adjusted memory, running state,
  completed Nginx request counts and latency histograms, Redis queued jobs,
  and sanitized warning/error events. Collection runs every 10 seconds.
- `compose.yml`: collector only; reuses the existing `docker-gateway:latest`
  Python runtime, without executing the Gateway entrypoint or migrations.
- `datasources.yml`, `dashboards.yml`: dedicated Grafana data sources and folder.
- `install.py`: preserves existing Prometheus jobs and Grafana data, backs up
  changed configuration, reloads Nginx/Prometheus, and restarts Grafana once.

API metrics describe requests completed through Nginx, including proxy overhead.
Streaming requests appear only after completion. Existing access-log exclusions
for public webhooks and connector tests remain in force. There are no per-route,
per-organization, per-user or per-workflow labels. Direct Gateway calls bypassing
Nginx are outside this dashboard's request metrics.

Queues are Redis DB 0 lists for `workflow`, `log`, `knowledge`, and `celery`,
including Kombu's default priority buckets (0, 3, 6, 9). Counts describe waiting
jobs, not reserved or executing jobs. Redis errors omit queue values rather than
reporting an incorrect zero. Container running status is not application health.

Warnings/errors are recognized from Python/Celery and Nginx severity prefixes.
Only service, severity, event count and a fixed description are sent to Loki;
original messages and tracebacks stay in Docker. Unknown log formats are ignored.
This dashboard does not ship request bodies, URLs, query strings, identities,
headers, credentials, or raw logs. Failed Loki pushes retry a bounded queue of
1,000 sanitized event batches, with an exported dropped-batch counter.

Counters/cursors are process-local and start at collector startup; restarting it
resets counters and does not recover downtime logs. Prometheus rate queries handle
counter resets. A Docker log response over 16 MiB fails that collection rather
than silently truncating it. Data retention follows the existing Prometheus/Loki
configuration; this setup does not change storage or retention policies.

## Authority and access

The collector runs as root with a read-only Docker socket mount. **Read-only
mounting does not restrict Docker API operations**: possession of this socket
grants daemon authority. The program itself only makes GET requests for inventory,
statistics and logs, and exposes only `/metrics` on an unpublished container port.
This trust boundary must be accepted explicitly before deployment. Do not use this
configuration in a shared or production environment without a restricted API
boundary. No Docker socket is exposed over TCP.

The collector joins the Nodease internal network and a dedicated internal
monitoring network. The existing application containers gain no new network or
Internet route. Grafana, Prometheus and Loki retain their existing networks,
ports, authentication settings and data.

## Install / reapply

Requires the six named application/monitoring containers checked by the installer
to be running, the local Gateway image, and the repository Gateway venv (PyYAML).
After accepting the Docker socket access and brief Grafana restart, run from the
repository root:

```bash
apps/gateway/.venv/bin/python docker/monitoring/install.py
```

Open [Nodease Local Operations](http://localhost:3000/d/nodease-local/nodease-local-operations).
Allow approximately 30–60 seconds and send normal Nodease API traffic before
expecting rates/percentiles. No traffic means no latency percentile, not zero
latency. Collection status panels distinguish missing data from healthy values.

Configuration backups are in `local/monitoring-backups/<timestamp>/`, beneath a
private directory. For rollback, stop the collector with the following command,
then restore the saved Nginx/Prometheus files and their prior Grafana provisioning
files (or remove only the newly added `nodease.yml` files if none existed), and
reload/restart the affected monitoring services. The shared containers and
application data volumes must be preserved.

```bash
docker compose -p nodease-monitoring -f docker/monitoring/compose.yml stop
```

Docker network attachments and copied provisioning/configuration files survive
container restart, but not replacement of the existing monitoring containers.
Reapply the installer after replacing them or recreating the Nodease network.
Nginx source configuration is tracked in `docker/nginx/nginx.conf`; rebuilding
its normal image includes the safe access-log format.

## Verification and boundaries

```bash
apps/gateway/.venv/bin/python -m pytest tests/ci/test_nodease_monitoring.py -q
docker compose -p nodease-monitoring -f docker/monitoring/compose.yml config --quiet
```

Before declaring the connection ready, check Prometheus's `nodease` target and
query its metrics; verify the two Grafana data sources and dashboard; generate
only benign API health traffic; and verify sanitized Loki ingestion. Provisioning
alone is not evidence that live metrics work.

| Protected-resource boundary | Disposition |
| --- | --- |
| Save/reference | Not applicable: no credentials or resource references stored |
| Management API/UI | Local Grafana access follows existing authentication; no Nodease permissions changed |
| Preflight | Installer checks required running containers and validates Nginx/Prometheus configuration |
| Runtime/background | GET-only Docker client; fixed local Redis queue names; internal `/metrics` only |
| Lifecycle | Missing containers report stopped; failed reads omit stale metrics; counters reset on restart |
| Audit/redaction | Allowlisted log metadata only; raw content discarded before Loki; Docker authority requires explicit acceptance |
| Tests | Redaction, malformed metrics, histogram semantics, log framing/boundaries, queue priorities, failure and retry tests |

Installed locally on 2026-09-05 after explicit authorization of Docker socket
access and the monitoring restart. Verified the Prometheus `nodease` target as
UP, all 11 Nodease containers, CPU/memory and four Redis queues, five benign API
health requests counted exactly, and a real sanitized log-system event in Loki.
The dashboard was opened successfully using the existing Chrome Grafana session.
The dashboard explicitly includes a 15-second refresh option because Grafana's
default interval list otherwise turns that configured interval off.

Unit verification: 11 tests passed. Compose/JSON/YAML parsing, Python compilation,
and diff whitespace validation passed. Ruff was unavailable in the local venv;
the full repository, database and E2E regression suites were not run because
application business code and database contracts did not change.
