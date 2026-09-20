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
| `full` | Restore all relevant audited paths (same path set as selected in v0.3) |

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

- Restores only into a BackupLint-owned temporary directory with a sentinel file
- Refuses destinations that are `/`, home roots, the repository, or live audited paths
- Cleans up only owned destinations after verification
- Uses argv-list Restic execution (no shell)
- Never runs prune/forget/repair/unlock

## Results

Restore verification appears as its own text section and JSON object
(`restore_verification`). A failed restore validation yields overall `FAIL`
(exit `1`). Auth/lock/missing/timeout yield exit `2`.
