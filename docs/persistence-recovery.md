# Persistence backup, recovery, and migration

BackupLint keeps durable state in SQLite for the fleet controller and the local
scheduler. This document describes **internal** backup/recovery semantics added
for upgrade confidence. It is not a complete disaster-recovery product manual.

## Stores

| Store | Module | Schema version | Contents |
|-------|--------|----------------|----------|
| Controller | `backuplint.fleet.controller_store` | `STORE_SCHEMA_VERSION` (currently 2) | agents, hashed enroll tokens, submissions, events, heartbeats |
| Schedule | `backuplint.schedule_store` | `SCHEDULE_STORE_SCHEMA_VERSION` (currently 1) | runs history, job_state |

Both use:

- `isolation_level=None` (explicit transactions)
- `PRAGMA journal_mode=WAL`
- `PRAGMA synchronous=NORMAL`
- `PRAGMA busy_timeout=5000`
- `PRAGMA foreign_keys=ON`
- process-local `threading.RLock`

**Unsupported:** two processes opening the same database file as writers.
Concurrent threads inside one process are supported via the store lock.

## What a DB backup includes

Use the internal APIs:

- `ControllerStore.backup(path)`
- `ScheduleStore.backup(path)`
- shared helper `backuplint.sqlite_backup.backup_sqlite_database`

Backups use SQLite’s online backup API against the live connection. They do
**not** require copying `-wal` / `-shm` sidecar files manually.

Each successful backup also writes `*.manifest.json` with safe metadata:

- store type
- schema version
- creation timestamp
- application version
- integrity-check result
- backup byte size
- notes

Manifests never include enrollment tokens, passwords, or private keys.

## What a DB backup does **not** include

Controller disaster recovery additionally requires files **outside** SQLite,
typically under the controller data directory:

- CA certificate / CA private key material
- per-agent client certificates and private keys
- any TLS material managed beside the DB

Schedule DB backups do not include:

- schedule lock files
- compose files, restic passwords, or host configuration

## Backup verification

`verify_sqlite_backup` / `ControllerStore.verify_backup` /
`ScheduleStore.verify_backup` open the backup read-only and check:

- `PRAGMA integrity_check`
- schema version readability
- required tables present
- store-specific logical invariants (agent statuses, submission/event consistency,
  schedule check_type / timestamp parseability)

A backup is not considered successful unless verification passes before the
atomic rename into the final destination.

## Safe restore sequence

Restore is an **offline** operation for isolated/test or stopped-process paths:

1. Stop the controller or scheduler process that owns the DB.
2. Preserve the backup file (and its manifest).
3. Validate the destination path (do not point at an unexpected live DB).
4. Run `restore_sqlite_backup_file(backup, dest, store_type=...)`.
5. Restore permissions (`0600` attempted by the helper).
6. Re-run verification / open the store and confirm expected records.
7. Separately restore CA/cert material if recovering a controller host.

There is **no** supported “restore over a live open ControllerStore” workflow.

## Migration support rules

- Migrations are ordered and version-checked.
- Newer-than-supported schema versions fail closed at startup.
- Migration steps run inside `BEGIN IMMEDIATE` where SQLite permits.
- Injected mid-migration failures roll back; the next clean startup retries.
- Supported upgrade paths today:
  - controller: v1 (pre-events) → v2 (current)
  - controller: v2 → v2 no-op
  - schedule: pre-meta / v1 → v1 current

## Diagnostics foundation

`diagnose_sqlite_file` / `store.diagnostics()` report existence, openability,
schema support, integrity, journal mode, required tables, and permission hints.
These are intended as building blocks for a future `backuplint doctor` command;
that CLI is not shipped by this phase.

## Recovery limitations

- SQLite backup ≠ full controller host DR (certs/CA excluded).
- Multi-process DB sharing is unsupported.
- Online restore while writers hold the DB is unsupported.
- Integrity_check proves SQLite structure, not full product semantics.
