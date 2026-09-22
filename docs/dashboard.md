# BackupLint dashboard (v1)

Optional **read-only** central web UI for fleet visibility. It runs on the
fleet controller HTTPS listener and never opens SQLite from the browser.

## Enable

1. Set an operator password (stored as scrypt hash, mode `0600`):

```bash
backuplint controller dashboard-password --data-dir /path/to/controller-state
```

2. Run the controller with the dashboard enabled:

```bash
backuplint controller run --data-dir /path/to/controller-state \
  --listen 127.0.0.1:8443 --hostname controller.example \
  --dashboard
```

Open `https://controller.example:8443/dashboard/` and sign in.

### Install profile

Feature id: `fleet_dashboard` (optional).

| Role | Default | Notes |
|------|---------|-------|
| controller | off | selectable with `fleet_controller` |
| all_in_one | off | selectable with `fleet_controller` |
| agent | not applicable | not installed |
| standalone | not applicable | not required |

Runtime config does not silently install dashboard dependencies. Enabling the
feature/profile only marks the component; `--dashboard` (or container env)
activates the HTTP routes.

### Container

Set `BACKUPLINT_CONTROLLER_DASHBOARD=1` and ensure
`/state/dashboard/password.scrypt` exists (or pass
`BACKUPLINT_DASHBOARD_PASSWORD_FILE`). Persist `/state` as usual.

## Auth model (v1)

- Shared operator password (scrypt hash on disk, mode `0600`)
- HttpOnly session cookie + SameSite=Strict (+ Secure on HTTPS)
- CSRF token required for logout
- Login rate limiting per client address
- No credentials in URLs
- API under `/v1/dashboard/*` requires the same session
- Agent private keys and enrollment tokens are never exposed

There is **no multi-user RBAC** and **no SSO** in v1.

## Health semantics

| UI field | Meaning |
|---------|---------|
| Presence online/stale/offline | Heartbeat freshness only |
| Current audit | Latest `audit.completed` by `occurred_at` |
| Last received | Latest ingest / `received_at` |
| Capabilities | Installed/supported/available — **not** PASS/FAIL |
| DATA_GAP / QUEUE_OVERFLOW | Explicit queue markers, never silent drops |

An agent can show **PASS** audit and **offline** presence at the same time.

## Service assurance (project / service / mount)

The dashboard projects **stored** `audit.completed` payloads. It does not poll
Docker, and it is not a generic container health monitor. Container running is
not the same as backed up.

| Field | Meaning |
|------|---------|
| Persistent data | Bind mounts and named volumes that BackupLint did not skip as cache/temporary/infrastructure |
| Coverage | Per-mount: protected / not protected / stale / skipped / unsupported |
| Freshness | Per-mount only when the finding includes snapshot evidence or a stale coverage status. Otherwise **UNKNOWN** or **NOT APPLICABLE** — never a fake PASS |
| Integrity | Repository-scoped (`restic check` / configured integrity mode). Shown on each service for context, not as a per-mount PASS |
| Restore verification | Repository-scoped isolated restore. **NOT RUN** if absent. `restic check` is not a restore |
| Overall | Worst of that service's persistent coverage/freshness plus repository integrity/restore. Sibling services stay independent for coverage FAIL |

Explicit non-success placeholders: **NOT RUN**, **UNKNOWN**, **NOT APPLICABLE**.
PASS is never shown for a check that did not run.

Repository/operational **ERROR** (timeout, auth, unavailable) is attributed to
the repository, not as a coverage FAIL.

Compose project names appear when the audit finding includes them; otherwise
services are grouped under `compose`. Image identity is shown when present in
the payload.

### Pages

- Fleet overview: presence, audit, persistent-service counts, alert counts
- Agent detail: `/dashboard/agents/{id}` — service table, attributed alerts, last 25 audit transitions
- Service drill-down: `/dashboard/agents/{id}/services/{project}/{service}` (legacy `?project=` still accepted)
- Alert links use the same project-aware URL and may include `?mount=` to highlight the failing path

Infrastructure binds such as `/etc/localtime`, `/etc/timezone`, the Docker socket, and
read-only host CA/zoneinfo mounts are classified as **infrastructure** and skipped
unless listed in `backup_paths`. Arbitrary `/etc/*` app config remains persistent.

## API (authenticated)

- `GET /v1/dashboard/overview`
- `GET /v1/dashboard/agents` (filters + pagination; includes `assurance` counts)
- `GET /v1/dashboard/agents/{id}` (includes `assurance` tree)
- `GET /v1/dashboard/agents/{id}/services/{name}`
- `GET /v1/dashboard/events` (bounded history)

## Network / TLS

Dashboard shares the controller TLS certificate. Prefer reverse-proxy TLS
termination only if cookies remain Secure and sessions are not forwarded
insecurely. Do not expose debug endpoints publicly.

## Read-only-first limitations

- No remote commands, restore/cutover controls, or config push
- No enterprise SSO/RBAC in v1
- Sessions are in-process (re-login after controller restart)
- Notifications are minimal
