# How BackupLint Works

BackupLint is being built in small milestones. This document explains the intended design in plain language and will grow as features land.

## The problem

Docker Compose services often store important data in bind mounts or named volumes. Backups only protect the paths they are told to include. If a new service adds `/srv/vaultwarden` and the backup job still only covers `/srv/docker`, that service is unprotected.

## The intended workflow

```text
Docker Compose configuration
          +
Configured backup paths
          ↓
      BackupLint
          ↓
PASS / WARN / FAIL report
```

1. Read the Compose project configuration through Docker's resolved config.
2. Discover mounts and classify obvious cache/temporary storage.
3. Compare persistent bind-mount host paths with configured `backup_paths`.
4. Report protected paths, missing coverage, skipped mounts, and unsupported cases.

## Current status

```bash
backuplint scan compose.yml
backuplint scan compose.yml --config backuplint.yml
```

Example:

```text
BackupLint Audit

✓ sonarr       /srv/docker/sonarr  protected
✗ vaultwarden  /srv/vaultwarden    not protected
```

Exit code `1` means at least one persistent bind mount is unprotected.

## How Compose configuration is obtained

BackupLint asks Docker:

```bash
docker compose -f <file> config --format json
```

Relative bind mounts are expanded to absolute host paths based on the Compose file location.

## How mounts are classified

- `tmpfs` mounts are `temporary`
- targets such as `/cache` or `/var/cache` are `cache`
- targets under `/tmp`, `/var/tmp`, `/run`, and `/dev/shm` are `temporary`
- ordinary bind mounts and named volumes default to `persistent`
- uncommon mount types remain `unknown`

Cache and temporary mounts are skipped during coverage checks. Uncertain bind mounts stay in scope.

## How backup coverage is determined

`backuplint.yml` lists filesystem roots that your backup job includes:

```yaml
backup_paths:
  - /srv/docker
```

Optional top-level `schema_version: 1` marks the configuration schema. If omitted, BackupLint treats the file as legacy schema 1 (current v0.5). Unsupported future versions are rejected rather than guessed.

For each persistent bind mount host path, BackupLint checks whether any configured backup path:

1. exactly matches the host path, or
2. is a parent directory of the host path

Parent detection uses pathlib semantics, not string prefixes. That means:

```text
backup path: /srv/app
mount path:  /srv/app2
result:      not protected
```

A child backup path also does not cover a parent mount. Backing up `/srv/docker/sonarr` does not protect `/srv/docker`.

Broken symlinks on bind sources are treated as unresolved. BackupLint reports a warning rather than claiming coverage.

## Named volumes

For named volumes, BackupLint asks Docker for the volume mountpoint:

```bash
docker volume inspect <name> --format '{{.Mountpoint}}'
```

That host path is then compared with `backup_paths` using the same parent/child rules as bind mounts. If the volume does not exist locally, BackupLint reports `review required` instead of guessing.

Unused volumes declared in Compose but not mounted by any service are ignored. BackupLint audits mounts that services actually use.

## Read-only rule

BackupLint inspects configuration and reports findings. It does not change Compose files, Docker volumes, or backup repositories.

## Restic snapshots and integrity

When `restic` is configured, BackupLint runs:

```bash
restic snapshots --json
```

Configured `restic.password_file` always overrides ambient `RESTIC_PASSWORD` /
`RESTIC_PASSWORD_FILE` from the process environment. Ambient credentials are used
only when the config does not supply a password file.

For each audited host path, BackupLint selects the newest snapshot whose backup roots cover that path. A newer snapshot of an unrelated root (for example `/etc`) does not hide an older snapshot that covers `/srv/docker`.

Optional `max_backup_age` compares that relevant snapshot's timestamp to now (UTC). Fresh coverage is protected; stale coverage is a warning; no covering snapshot is critical.

When integrity is enabled (`restic.integrity.mode` or `--integrity`), BackupLint also runs
`restic check` (standard) or `restic check --read-data` (deep) **once per repository per
scan**. See [integrity-checks.md](integrity-checks.md).

Failure modes:

- no snapshots / no covering snapshot → mounts reported not protected
- wrong/missing password → exit code `2` without printing the password
- inaccessible repository → exit code `2`
- malformed JSON → exit code `2`
- missing snapshot timestamp with `max_backup_age` set → warning
- integrity inconsistency → overall `FAIL` (exit `1`)
- integrity operational error (auth, lock, timeout) → exit `2`

## Database warnings

If a service image is exactly `postgres`, `postgresql`, `mysql`, `mariadb`, `mongo`, or `mongodb` (optional registry/tag), BackupLint adds a warning:

```text
⚠ postgres  database detected  review required
  Database workload detected (postgres). Verify that your backup method provides a consistent database backup.
```

This does not claim filesystem backups are invalid. It reminds operators to verify consistency for database workloads. Similarly named images such as `postgres-exporter` are ignored.

## Audit results

Each scan ends with `Result: PASS`, `WARN`, or `FAIL`:

- `PASS`: all evaluated persistent bind mounts are protected and requested integrity passed (or was not requested)
- `WARN`: no critical gaps, but something needs review (stale snapshots, unresolved volumes, stale remembered integrity)
- `FAIL`: at least one persistent bind mount is not covered, or requested integrity reported repository inconsistency

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | `PASS` or `WARN` |
| 1 | `FAIL` |
| 2 | Configuration or runtime error |

## Restic restore verification

When restore verification is enabled (`restic.restore_verification.mode` or
`--restore-verify`), BackupLint performs a real `restic restore` of relevant
audited paths into an isolated temporary directory and validates restored
content. This is separate from coverage and integrity checks and never writes
over live application data. See [restore-verification.md](restore-verification.md).
