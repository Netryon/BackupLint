# Architecture and deployment modes

BackupLint is an independent **backup-assurance** layer. It inspects Compose mounts and optional Restic metadata; it does not run your backup jobs.

```text
Compose host(s)                         Optional fleet
─────────────────                       ─────────────────────────────
docker compose config                   Agent  --HTTPS + mTLS-->  Controller
backup_paths and/or Restic snapshots                              SQLite + CA
optional restic check / restore-verify                            optional dashboard
local daemon / systemd scheduler                                  optional SIEM export
                                                                  central policy store
```

## What lives where

| Piece | Owns | Does not own |
| --- | --- | --- |
| Standalone CLI | Local scan, local schedule | Fleet identity |
| Agent | Private key, durable submit queue, local Compose/Restic access | Backup repositories on the controller |
| Controller | Fleet CA, enrollment, results, policy, dashboard, SIEM queue | Docker socket, Restic passwords, Compose files |

Agents open **outbound** HTTPS only. The controller does not SSH to hosts and does not store Restic repository passwords.

## Deployment modes

1. **Standalone** — `backuplint scan` on the Compose host.
2. **Native controller + native agent** — systemd or foreground processes; CSR enrollment.
3. **Container controller + container agent** — uid 10001, `/state` volumes, no Docker socket by default.
4. **Mixed** — any combination of native and container forms against one controller.

Installation profiles (`standalone`, `agent`, `controller`, `all_in_one`) select features without splitting the package. See [installation-profiles.md](installation-profiles.md).

## Assurance states (keep these distinct)

```text
coverage ≠ integrity ≠ restore verification ≠ disaster recovery
presence (online / stale / offline) ≠ audit health
coverage FAIL ≠ repository-unavailable ERROR
```

- **Coverage** — do configured paths or Restic snapshot roots cover persistent Compose mounts?
- **Integrity** — `restic check` (standard) or `restic check --read-data` (deep). Deep is still not a restore.
- **Restore verification** — isolated restore into BackupLint-owned temp paths. Not application consistency, not a DR drill.
- **ERROR** — operational problems such as a missing or unreachable repository. Not the same as a coverage **FAIL**.
- **Presence** — last heartbeat. An online agent can still have a FAIL audit; a stale agent can still have an old PASS on file.

## Fleet protocol

Enrollment: one-time token + agent-generated CSR. After enrollment, unique client certificates (mTLS). Revocation is a controller operation.

Agents report software version and protocol version. The controller accepts the current protocol and the immediately previous one; older or newer versions are rejected. See [fleet.md](fleet.md).

## Central policy

Immutable revisions, assignment, drift, and staged rollout. Newly enrolled agents may need an explicit assignment before policy apply reports success. The dashboard is read-only for policy. See [central-policy.md](central-policy.md).

## SIEM

Optional HTTPS JSON export with a durable queue so receiver outages do not drop the local audit trail. Export health is not the same as backup PASS. See [siem.md](siem.md).
