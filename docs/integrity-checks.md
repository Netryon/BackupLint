# Restic integrity checks

BackupLint can optionally verify **Restic repository integrity** in addition to
coverage and snapshot freshness.

This is **not** restore verification. A successful integrity check does not prove
that a restore will succeed, that applications will start from restored data, or
that database files are transaction-consistent.

## Modes

| Mode | Command | What it checks |
| --- | --- | --- |
| `off` (default) | *(none)* | Pre-integrity behavior: coverage/freshness only |
| `standard` | `restic check` | Repository structure, indexes, trees, and packs metadata |
| `deep` | `restic check --read-data` | Standard checks **plus** reading all data blobs |

`deep` is opt-in, I/O-heavy, and can be slow on large, remote, HDD, or Raspberry Pi
/ SD-card repositories. Do not enable deep mode on every routine scan unless you
intentionally want that cost.

BackupLint runs at most **one** integrity check per configured repository per
scan, not once per mount.

## Configuration

```yaml
backup_paths: []

restic:
  repository: /var/backups/restic
  password_file: ./restic.pass
  integrity:
    mode: standard          # off | standard | deep
    max_age: 7d             # optional; used when mode is off and state_file is set
    state_file: ./.backuplint-integrity.json
```

CLI override (wins over config):

```bash
backuplint scan compose.yml --integrity off
backuplint scan compose.yml --integrity standard
backuplint scan compose.yml --integrity deep
```

## Outcomes

| Integrity result | Typical overall effect |
| --- | --- |
| passed | Does not downgrade a coverage PASS/WARN |
| failed (inconsistency) | Overall **FAIL**, exit `1` |
| operational error (auth, missing repo, lock, timeout) | Exit `2` |
| stale remembered success (`max_age` + `state_file`, live check off) | **WARN** when coverage is otherwise PASS |

## State file

When `state_file` is set, a successful live integrity check updates local metadata:

- no passwords or password-file contents
- repository identity is a hash (remote userinfo is stripped first)
- atomic replace with restrictive permissions (`0600` when the OS allows)

Failures do not rewrite the last successful timestamp as success.

## Examples

### Integrity disabled (default)

```yaml
restic:
  repository: /var/backups/restic
  password_file: ./restic.pass
```

### Standard integrity PASS

```text
Restic integrity
✓ standard integrity check passed        standard        1.2s

Result: PASS
```

### Integrity FAIL

```text
Restic integrity
✗ standard integrity check failed        repository inconsistency detected

Result: FAIL
```

### JSON

```bash
backuplint scan compose.yml --integrity standard --json
```

Includes an `integrity` object with `requested`, `mode`, `status`,
`duration_seconds`, and `message` (plus optional `checked_at` /
`restic_version`).
