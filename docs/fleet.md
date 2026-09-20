# Fleet controller and agent (v0.5)

BackupLint can report local verification results to a central controller.

## Product statement

> BackupLint checks all my servers and reports their state centrally.

## Architecture

```text
BackupLint Agent  --HTTPS/TLS + mTLS-->  BackupLint Controller
```

- No UDP, no inbound agent ports, no remote shell, no remote Docker/Restic credentials on the controller.
- v0.4 scheduler still runs checks locally; the agent submits result envelopes.
- Central policy / GitOps (v0.8): see [central-policy.md](central-policy.md).
- Operational history retention: `backuplint controller prune-history` (never deletes `policy_audit`); see [controller-recovery.md](controller-recovery.md).

## Quick lab

```bash
# Controller host
# Use --hostname matching how agents will connect (DNS name or IP).
# That value is included in the server certificate SAN.
backuplint controller init --data-dir ./bl-controller --hostname localhost
backuplint controller run --data-dir ./bl-controller --listen 127.0.0.1:8443 &
TOKEN=$(backuplint controller enroll-token --data-dir ./bl-controller --label web1)
# Prints:
#   agent_id=agent-...
#   token=...
AGENT_ID=$(printf '%s\n' "$TOKEN" | sed -n 's/^agent_id=//p')
ENROLL_TOKEN=$(printf '%s\n' "$TOKEN" | sed -n 's/^token=//p')

# Agent host (copy CA first)
backuplint agent enroll \
  --controller https://127.0.0.1:8443 \
  --token "$ENROLL_TOKEN" \
  --agent-id "$AGENT_ID" \
  --ca-cert ./bl-controller/ca/ca.crt \
  --identity-dir ./bl-agent

backuplint agent submit compose.yml -c backuplint.yml \
  --controller https://127.0.0.1:8443 \
  --identity-dir ./bl-agent

backuplint controller agents --data-dir ./bl-controller
```

## Provisioning

Generic (provider-neutral) provisioning bundles are created with:

```bash
backuplint controller provision-batch \
  --data-dir ./bl-controller \
  --count 100 \
  --controller-url https://controller.example:8443 \
  --output ./provisioning-bundle.json
```

The bundle is JSON (mode 0600) with per-agent `agent_id`, one-time `enrollment_token`,
controller URL, optional CA PEM, role, and feature profile. Vendor adapters
(Ansible/AWX/cloud-init) are intentionally deferred.

## Capabilities

Agents report a versioned capability document on heartbeat (`schema_version`,
`deployment_form`, and per-family `installed` / `supported` / `available` /
`reason`). Controllers store this separately from audit PASS/FAIL.

`NOT_INSTALLED` / `UNSUPPORTED` / `UNAVAILABLE` reasons are **not** FAIL.
Unknown capability keys from newer agents are preserved without crashing the controller.

## Protocol compatibility

Agents report both BackupLint **software version** (`backuplint_version` on envelopes)
and **fleet protocol version** (`protocol_version`).

Controller policy (rolling upgrades):

| Agent protocol | Result |
|----------------|--------|
| current (2) | accepted |
| immediately previous (1) | accepted |
| older than supported window | rejected (`PROTOCOL_UNSUPPORTED_TOO_OLD`) |
| newer than controller | rejected (`PROTOCOL_UNSUPPORTED_TOO_NEW`) |
| malformed | rejected (`PROTOCOL_MALFORMED`) |

`GET /v1/health` returns `protocol_version`, `software_version`, and
`supported_protocol_versions`. Negotiation lives in `backuplint.fleet.compat`
(not scattered through handlers). Unknown future envelope fields are not
interpreted optimistically.

## Security notes

- TLS 1.2+ with server certificate verification (no plaintext fallback).
- Enrollment uses a short-lived one-time token plus an agent-generated CSR; the agent private key never leaves the agent. Token values are not logged.
- After enrollment, agents authenticate with unique client certificates (mTLS).
- Revoke with `backuplint controller revoke <agent_id>`.
- Result envelopes reject obvious secret-bearing keys.
- Agent queue is durable, bounded, and mode `0600`.

## Stale / offline

Controller tracks last heartbeat and last result timestamps separately from backup PASS/FAIL.
An old PASS does not mean the agent is currently healthy.

## Current state vs last received

Two distinct store helpers:

| Helper | Ordering | Use for |
|--------|----------|---------|
| `current_event_by_occurred_at` / `current_result_by_occurred_at` | `occurred_at` / `scan_time` (tie: receive time, id) | Dashboard / backup health |
| `latest_event` / `latest_result` | `received_at` / `received_time` | Ingest lag, last-received debug |

Invariant: an older scan that arrives later must **not** overwrite newer current health.
Heartbeat freshness is never mixed into current audit state.

## Identity

- `agent_id` is the stable identity (UUID-based). Hostname and display label are editable metadata.
- Renaming a host or label does not create a new agent.
- Each audit submission has a unique `submission_id` (idempotency) and a `run_id` (correlation).
- Controllers store a versioned canonical event (`schema_version`) with distinct `occurred_at` and `received_at`.

## Optionality

Fleet is optional. `backuplint scan` and local scheduling work with no `fleet:` section, no controller, and no network.

## Containerized controller

The controller can run in a container without Docker socket, Restic, or host root mounts.
See [controller-container.md](controller-container.md) for image build, `/state` persistence,
hardened Compose (`compose.hardened.yml`), healthcheck, SIGTERM behavior, and upgrade notes.

## Optional dashboard (v0.6)

A read-only operator UI can be enabled on the controller (`--dashboard`).
See [dashboard.md](dashboard.md) for auth, semantics, install-profile feature
`fleet_dashboard`, and container env `BACKUPLINT_CONTROLLER_DASHBOARD`.

## Optional SIEM export (v0.7)

Normalized failure/security events can be exported to an external collector.
See [siem.md](siem.md) for configuration, install-profile feature
`siem_export`, controller `--siem-config`, and container env
`BACKUPLINT_SIEM_CONFIG_FILE`.
