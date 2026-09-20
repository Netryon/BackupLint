# Controller backup and recovery (pre-v1)

This procedure recovers a BackupLint fleet controller from a disposable copy of
operator-managed backups. It never mutates preserved raw evidence stores.

## What to back up

| Asset | Typical path under `--data-dir` | Notes |
|-------|----------------------------------|-------|
| Controller DB | `controller.sqlite3` (+ `-wal`/`-shm` if present) | Prefer `backuplint controller` online backup API / `ControllerStore.backup()` |
| CA / trust | `ca/` | Private CA material; 0600/0700 |
| Server TLS | `server/` | Server cert/key |
| Agent certs metadata | `agents/` (if used) | Enrollment artifacts |
| Dashboard auth | password hash / session secrets under data-dir | Required for dashboard login |
| SIEM queue/state | `siem/` | Queue + telemetry; resume after restore |
| Policy state | inside controller DB (schema v7+) | policies, assignments, drift, rollouts, `policy_audit` |

## Backup

1. Stop or quiesce writers if taking a cold copy; otherwise use the online SQLite backup helper.
2. Copy the assets above to a dated recovery set with restrictive permissions (`0700` dirs, `0600` files).
3. Record schema version and application version from the backup manifest.

## Restore (disposable target only)

1. Provision an empty data-dir.
2. Restore files with original relative layout and ownership (service user `backuplint` when used).
3. Ensure permissions: data-dir `0700`, sqlite/certs `0600`.
4. Start controller; confirm `/v1/health`.
5. Agents reconnect over mTLS using the restored CA (same CA required).
6. Dashboard login works with restored password hash.
7. SIEM exporter resumes from restored queue.
8. Policy desired/observed rows remain coherent (`backuplint controller policy list`, dashboard Policy page).

## Verification checklist

- `PRAGMA integrity_check` OK
- schema_version supported
- sample agent heartbeat + policy desired pull
- dashboard overview loads
- SIEM status endpoint healthy or correctly degraded
- `policy_audit` history present (never pruned by operational history retention)

## Non-goals

- Cross-CA identity migration
- Partial DB row salvage without integrity_check
- Silent deletion of `policy_audit`
