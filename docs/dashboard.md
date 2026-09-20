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

## Network / TLS

Dashboard shares the controller TLS certificate. Prefer reverse-proxy TLS
termination only if cookies remain Secure and sessions are not forwarded
insecurely. Do not expose debug endpoints publicly.

## Read-only-first limitations

- No remote commands, restore/cutover controls, or config push
- No enterprise SSO/RBAC in v1
- Sessions are in-process (re-login after controller restart)
- Notifications are minimal

## API (authenticated)

- `GET /v1/dashboard/overview`
- `GET /v1/dashboard/agents` (filters + pagination)
- `GET /v1/dashboard/agents/{id}`
- `GET /v1/dashboard/events` (bounded history)
