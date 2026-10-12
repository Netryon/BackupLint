# Restic restore verification

BackupLint can optionally restore selected backup paths from Restic into an
**isolated temporary directory** and validate that the restored content exists
and is readable.

This is **not** a full disaster-recovery drill and does **not** restore over live
application data.

```text
coverage != integrity
integrity != restore verification
restore verification != full disaster recovery
successful file restore != successful database recovery
```

## Modes

| Mode | Meaning |
| --- | --- |
| `off` (default) | No restore verification |
| `selected` | Restore relevant audited paths from the relevant snapshot(s) |
| `full` | Currently restores the same relevant audited path set as `selected`; it does **not** mean whole-repository, whole-host, application-level, or disaster-recovery restore |

Integrity modes (`standard` / `deep`) are separate and unchanged.

## Configuration

```yaml
restic:
  repository: /var/backups/restic
  password_file: ./restic.pass
  restore_verification:
    mode: selected          # off | selected | full
    timeout: 30m            # optional
    expected_paths:         # optional relative paths under restored roots
      - config.json
```

CLI override:

```bash
backuplint scan compose.yml --restore-verify selected
```

## Safety

- Restores only into a unique BackupLint-owned temporary directory with restrictive permissions and a sentinel file
- Refuses sensitive/system destinations, home-directory roots, unsafe repository overlap, and live audited bind/volume paths
- If the process temporary-directory setting is unsafe, BackupLint uses a protected user-cache fallback instead of restoring under a sensitive system path
- Cleans up only a destination BackupLint created and whose sentinel still matches the current run
- Uses argv-list Restic execution (no shell)
- Never runs prune/forget/repair/unlock

## Results

Restore verification appears as its own text section and JSON object
(`restore_verification`). A failed restore validation yields overall `FAIL`
(exit `1`). Operational problems such as auth, repository access, lock, timeout,
or unsafe temporary-destination setup are `ERROR` conditions and CLI exit `2`.

A PASS means the requested audited paths were restored into the isolated
destination and the expected restored content was present/readable. It does not
prove the application can start from that data, that a database is
transaction-consistent, or that complete disaster recovery will succeed.
