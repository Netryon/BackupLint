# BackupLint SIEM export (v0.7)

Optional **best-effort** normalized event export to an external SIEM or
webhook collector. SIEM availability never changes backup-assurance truth,
scan exit codes, or fleet ingest HTTP responses.

## Enable

### Standalone

Add a `siem:` block to `backuplint.yml` (opt-in; disabled by default):

```yaml
siem:
  siem_config_schema_version: 1
  enabled: true
  transport: https_json
  endpoint: "https://siem.example.internal/ingest"
  auth:
    type: bearer
    token:
      source: env
      name: BACKUPLINT_SIEM_TOKEN
```

After `backuplint scan` or `backuplint schedule run`, failure events are
enqueued locally and a bounded `drain_once()` attempt runs immediately.
Use `backuplint siem flush` for manual/cron retry.

### Controller / all-in-one

Provide a SIEM YAML file when starting the controller:

```bash
backuplint controller run --data-dir /path/to/controller-state \
  --listen 127.0.0.1:8443 --hostname controller.example \
  --siem-config /path/to/siem.yml
```

Fleet result ingest enqueues exportable events on a background worker.
Query integration health over mTLS:

```bash
backuplint siem status --controller https://controller.example:8443 \
  --identity-dir /path/to/agent-identity
```

### Install profile

Feature id: `siem_export` (optional).

| Role | Default | Notes |
|------|---------|-------|
| standalone | off | local queue under schedule/XDG state |
| controller | off | queue under controller data dir |
| all_in_one | off | export via controller ingest only (no dual-send) |
| agent | not applicable | agents never export directly |

Runtime config does not silently install SIEM dependencies. Enabling the
feature/profile only marks the component; `siem.enabled: true` (or
`--siem-config`) activates the pipeline.

### Container

Set `BACKUPLINT_SIEM_CONFIG_FILE=/state/siem.yml` (and optionally
`BACKUPLINT_CONTROLLER_SIEM=1`). Persist `/state` as usual; the entrypoint
creates `/state/siem/` for the durable queue and telemetry.

## Auth model

- Bearer token via `SecretRef` (`env`, `file`, `systemd`, `mounted`) — never inline secrets
- TLS verification on by default (`tls.verify: true`)
- Tokens resolved at send time (rotation without controller restart)
- `/v1/siem/status` requires client certificate (same tier as `/v1/metrics`)

## Event model

Normalized `SiemEvent` records (schema version 1) with stable `event_id`
deduplication. Categories are explicit:

| Category | Examples |
|----------|----------|
| backup_assurance | coverage/integrity/restore failures |
| fleet_transport | agent offline/online, DATA_GAP, QUEUE_OVERFLOW |
| security_audit | authentication failures |
| controller_operational | controller faults (rate-limited) |
| siem_delivery | reserved; delivery errors stay local only |

PASS audit results are not exported as failure families. Raw scan JSON,
secrets, and controller DB dumps are never exported.

## Severity

Five levels: `critical`, `high`, `medium`, `low`, `info`. CRITICAL/HIGH
events are never filterable. Default `min_severity: low` exports everything
except pure INFO noise.

## Operational commands

```bash
backuplint siem flush [--state-dir PATH] [--config backuplint.yml]
backuplint siem status [--state-dir PATH | --controller URL --identity-dir PATH]
```

## Limitations (v0.7)

- HTTPS JSON transport only (one event per request)
- No syslog/CEF/vendor adapters yet
- No dashboard SIEM panel yet (`DashboardQueryService.siem_status()` stub only)
- SIEM outage queues events with backoff; extremely rare hard-ceiling overflow
  is logged locally

## Core semantic rule

BackupLint backup truth, scan exit codes, and fleet ingest responses are
independent of SIEM delivery. A broken SIEM integration must never be mistaken
for a backup failure.

See also: [Fleet](fleet.md), [Controller container](controller-container.md).
