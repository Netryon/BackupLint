"""Online-safe SQLite backup, verification, restore helpers, and diagnostics.

These primitives are internal foundations for controller/scheduler persistence
recovery. They do **not** define a public disaster-recovery product UX.

Important boundaries:
- Backups use SQLite's online backup API (not naive file copies of -wal/-shm).
- Controller DB backups do **not** include CA/private key material stored
  outside the SQLite file (typically under the controller data directory).
- Restoring a live DB while a store process holds it open is unsupported.
- One writer process per database file is supported; multi-process writers are not.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backuplint.sqlite_util import configure_store_connection, integrity_check_ok, journal_mode

CONTROLLER_REQUIRED_TABLES = frozenset(
    {
        "meta",
        "enroll_tokens",
        "agents",
        "submissions",
        "events",
        "heartbeats",
    }
)
SCHEDULE_REQUIRED_TABLES = frozenset({"meta", "runs", "job_state"})

CONTROLLER_NOTES = (
    "Controller SQLite backup includes agent registry, tokens (hashed), "
    "submissions, events, and heartbeats only.",
    "CA certificates, agent client keys/certs, and other files under the "
    "controller data directory are NOT included and must be backed up separately.",
)
SCHEDULE_NOTES = (
    "Schedule SQLite backup includes run history and job_state only.",
    "Schedule lock files and compose/restic configuration are NOT included.",
)


class PersistenceBackupError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class BackupManifest:
    store_type: str
    schema_version: int
    created_at: str
    application_version: str
    integrity_check: str
    backup_bytes: int
    backup_path: str
    notes: tuple[str, ...] = ()

    def to_json(self) -> str:
        payload = asdict(self)
        payload["notes"] = list(self.notes)
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True)
class BackupVerification:
    ok: bool
    store_type: str
    schema_version: int | None
    integrity_check: str
    tables_present: tuple[str, ...]
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class PersistenceDiagnostics:
    path: str
    exists: bool
    opens: bool
    store_type: str | None
    schema_version: int | None
    schema_supported: bool | None
    integrity_ok: bool | None
    journal_mode: str | None
    required_tables_present: bool | None
    permissions_mode: str | None
    issues: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _restrictive_chmod(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        # Best-effort on platforms that may not honor mode bits the same way.
        pass


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(r[0]) for r in rows}


def _meta_schema_version(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def _open_ro(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    return conn


def backup_sqlite_database(
    source_conn: sqlite3.Connection,
    dest_path: Path,
    *,
    store_type: str,
    schema_version: int,
    application_version: str,
    notes: Sequence[str] = (),
    pages_per_step: int = 100,
) -> BackupManifest:
    """Create a consistent online backup of an open SQLite connection.

    Writes to a temporary sibling file, verifies integrity, then atomically
    replaces the destination. On any failure, incomplete destinations are removed.
    """
    dest_path = Path(dest_path)
    if dest_path.exists() and dest_path.is_dir():
        raise PersistenceBackupError(f"backup destination is a directory: {dest_path}")
    tmp_path = dest_path.with_name(dest_path.name + ".tmp")
    manifest_path = Path(str(dest_path) + ".manifest.json")
    tmp_manifest = Path(str(manifest_path) + ".tmp")

    for stale in (tmp_path, tmp_manifest):
        if stale.exists():
            stale.unlink()

    dest_conn: sqlite3.Connection | None = None
    try:
        try:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PersistenceBackupError(
                f"cannot create backup directory {dest_path.parent}: {exc}"
            ) from exc
        dest_conn = sqlite3.connect(str(tmp_path), isolation_level=None)
        # Copy while source may be actively written (SQLite backup API).
        source_conn.backup(dest_conn, pages=pages_per_step)
        dest_conn.close()
        dest_conn = None
        _restrictive_chmod(tmp_path)

        verify = verify_sqlite_backup(tmp_path, store_type=store_type)
        if not verify.ok:
            raise PersistenceBackupError(
                "backup verification failed: " + "; ".join(verify.issues)
            )

        size = tmp_path.stat().st_size
        manifest = BackupManifest(
            store_type=store_type,
            schema_version=schema_version,
            created_at=_utc_now_iso(),
            application_version=application_version,
            integrity_check=verify.integrity_check,
            backup_bytes=size,
            backup_path=str(dest_path),
            notes=tuple(notes),
        )
        tmp_manifest.write_text(manifest.to_json(), encoding="utf-8")
        _restrictive_chmod(tmp_manifest)
        os.replace(tmp_path, dest_path)
        os.replace(tmp_manifest, manifest_path)
        _restrictive_chmod(dest_path)
        _restrictive_chmod(manifest_path)
        return manifest
    except Exception as exc:
        for path in (tmp_path, tmp_manifest):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
        # Do not leave a partial "successful" destination from this attempt.
        if isinstance(exc, PersistenceBackupError):
            raise
        raise PersistenceBackupError(f"backup failed: {exc}") from exc
    finally:
        if dest_conn is not None:
            try:
                dest_conn.close()
            except sqlite3.Error:
                pass


def verify_sqlite_backup(
    path: Path,
    *,
    store_type: str,
    max_supported_schema: int | None = None,
) -> BackupVerification:
    """Open a backup read-only and validate integrity + store-specific invariants."""
    path = Path(path)
    issues: list[str] = []
    if not path.is_file():
        return BackupVerification(
            ok=False,
            store_type=store_type,
            schema_version=None,
            integrity_check="missing",
            tables_present=(),
            issues=(f"backup file missing: {path}",),
        )

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_ro(path)
        integrity = "ok" if integrity_check_ok(conn) else "failed"
        if integrity != "ok":
            issues.append("PRAGMA integrity_check failed")
        tables = sorted(_table_names(conn))
        required = (
            CONTROLLER_REQUIRED_TABLES
            if store_type == "controller"
            else SCHEDULE_REQUIRED_TABLES
            if store_type == "schedule"
            else frozenset()
        )
        if required and not required.issubset(tables):
            missing = sorted(required - set(tables))
            issues.append(f"missing required tables: {missing}")

        schema_version = _meta_schema_version(conn)
        if schema_version is None:
            issues.append("schema_version missing or unreadable")
        elif max_supported_schema is not None and schema_version > max_supported_schema:
            issues.append(
                f"schema_version {schema_version} newer than supported "
                f"{max_supported_schema}"
            )

        if store_type == "controller":
            issues.extend(_controller_logical_issues(conn))
        elif store_type == "schedule":
            issues.extend(_schedule_logical_issues(conn))
        else:
            issues.append(f"unknown store_type {store_type!r}")

        return BackupVerification(
            ok=not issues,
            store_type=store_type,
            schema_version=schema_version,
            integrity_check=integrity,
            tables_present=tuple(tables),
            issues=tuple(issues),
        )
    except sqlite3.Error as exc:
        return BackupVerification(
            ok=False,
            store_type=store_type,
            schema_version=None,
            integrity_check="error",
            tables_present=(),
            issues=(f"cannot open backup: {exc}",),
        )
    finally:
        if conn is not None:
            conn.close()


def _controller_logical_issues(conn: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    if "agents" in _table_names(conn):
        rows = conn.execute("SELECT DISTINCT status FROM agents").fetchall()
        for (status,) in rows:
            if status not in {"active", "revoked"}:
                issues.append(f"unsupported agent status {status!r}")
    # Events that reference a submission_id should match a submissions row when present.
    if {"events", "submissions"}.issubset(_table_names(conn)):
        orphans = conn.execute(
            """
            SELECT COUNT(*) FROM events e
            WHERE e.submission_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM submissions s WHERE s.submission_id = e.submission_id
              )
            """
        ).fetchone()
        if orphans is not None and int(orphans[0]) > 0:
            issues.append(
                f"{orphans[0]} event(s) reference missing submission_id"
            )
        mismatched = conn.execute(
            """
            SELECT COUNT(*) FROM submissions s
            JOIN events e ON e.submission_id = s.submission_id
            WHERE s.event_id IS NOT NULL AND s.event_id != e.event_id
            """
        ).fetchone()
        if mismatched is not None and int(mismatched[0]) > 0:
            issues.append(
                f"{mismatched[0]} submission/event event_id mismatch(es)"
            )
    return issues


def _schedule_logical_issues(conn: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    tables = _table_names(conn)
    known = {"coverage", "integrity", "restore_verification", "deep_integrity"}
    if "runs" in tables:
        rows = conn.execute("SELECT DISTINCT check_type FROM runs").fetchall()
        for (check_type,) in rows:
            if check_type not in known:
                issues.append(f"unknown runs.check_type {check_type!r}")
    if "job_state" in tables:
        rows = conn.execute("SELECT DISTINCT check_type FROM job_state").fetchall()
        for (check_type,) in rows:
            if check_type not in known:
                issues.append(f"unknown job_state.check_type {check_type!r}")
        rows = conn.execute(
            """
            SELECT next_run, last_started, last_finished, last_success, last_failure
            FROM job_state
            """
        ).fetchall()
        for row in rows:
            for raw in row:
                if raw is None:
                    continue
                try:
                    datetime.fromisoformat(str(raw))
                except ValueError:
                    issues.append(f"unparseable schedule timestamp {raw!r}")
                    break
    return issues


def restore_sqlite_backup_file(
    backup_path: Path,
    dest_path: Path,
    *,
    store_type: str,
    max_supported_schema: int | None = None,
) -> BackupVerification:
    """Copy a verified backup into ``dest_path`` for offline restore tests.

    Prerequisites (caller responsibility):
    - no live store process has ``dest_path`` open
    - original backup file is preserved
    - destination path is an isolated/test path (not a live production DB)
    - post-restore verification is performed (this function verifies before copy)

    This intentionally does **not** provide an online in-place restore while a
    controller/scheduler holds the database open.
    """
    backup_path = Path(backup_path)
    dest_path = Path(dest_path)
    verify = verify_sqlite_backup(
        backup_path,
        store_type=store_type,
        max_supported_schema=max_supported_schema,
    )
    if not verify.ok:
        raise PersistenceBackupError(
            "refusing to restore unverified backup: " + "; ".join(verify.issues)
        )
    if dest_path.exists() and dest_path.is_dir():
        raise PersistenceBackupError(f"restore destination is a directory: {dest_path}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_path.with_name(dest_path.name + ".restore-tmp")
    if tmp.exists():
        tmp.unlink()
    try:
        data = backup_path.read_bytes()
        tmp.write_bytes(data)
        _restrictive_chmod(tmp)
        os.replace(tmp, dest_path)
        _restrictive_chmod(dest_path)
    except OSError as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise PersistenceBackupError(f"restore copy failed: {exc}") from exc
    return verify


def diagnose_sqlite_file(
    path: Path,
    *,
    store_type: str,
    max_supported_schema: int,
) -> PersistenceDiagnostics:
    """Reusable persistence diagnostics foundation (future ``doctor`` input)."""
    path = Path(path)
    issues: list[str] = []
    exists = path.exists()
    if not exists:
        return PersistenceDiagnostics(
            path=str(path),
            exists=False,
            opens=False,
            store_type=store_type,
            schema_version=None,
            schema_supported=None,
            integrity_ok=None,
            journal_mode=None,
            required_tables_present=None,
            permissions_mode=None,
            issues=("database file does not exist",),
        )

    mode: str | None
    try:
        mode = oct(path.stat().st_mode & 0o777)
        if path.stat().st_mode & 0o077:
            issues.append(f"permissions {mode} are group/world accessible")
    except OSError as exc:
        mode = None
        issues.append(f"stat failed: {exc}")

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(path), isolation_level=None)
        configure_store_connection(conn)
        opens = True
        jm = journal_mode(conn)
        integ = integrity_check_ok(conn)
        if not integ:
            issues.append("integrity_check failed")
        schema = _meta_schema_version(conn)
        supported: bool | None
        if schema is None:
            supported = False
            issues.append("schema_version missing")
        else:
            supported = schema <= max_supported_schema
            if not supported:
                issues.append(
                    f"schema_version {schema} newer than supported {max_supported_schema}"
                )
        tables = _table_names(conn)
        required = (
            CONTROLLER_REQUIRED_TABLES
            if store_type == "controller"
            else SCHEDULE_REQUIRED_TABLES
        )
        required_ok = required.issubset(tables)
        if not required_ok:
            issues.append(f"missing tables: {sorted(required - tables)}")
        return PersistenceDiagnostics(
            path=str(path),
            exists=True,
            opens=opens,
            store_type=store_type,
            schema_version=schema,
            schema_supported=supported,
            integrity_ok=integ,
            journal_mode=jm,
            required_tables_present=required_ok,
            permissions_mode=mode,
            issues=tuple(issues),
        )
    except sqlite3.Error as exc:
        issues.append(f"open failed: {exc}")
        return PersistenceDiagnostics(
            path=str(path),
            exists=True,
            opens=False,
            store_type=store_type,
            schema_version=None,
            schema_supported=None,
            integrity_ok=None,
            journal_mode=None,
            required_tables_present=None,
            permissions_mode=mode,
            issues=tuple(issues),
        )
    finally:
        if conn is not None:
            conn.close()


def wal_checkpoint(
    conn: sqlite3.Connection,
    mode: str = "PASSIVE",
) -> tuple[int, int, int]:
    """Run ``PRAGMA wal_checkpoint`` and return (busy, log, checkpointed)."""
    allowed = {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}
    if mode not in allowed:
        raise PersistenceBackupError(f"unsupported checkpoint mode {mode!r}")
    row = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
    if row is None:
        return (0, 0, 0)
    return (int(row[0]), int(row[1]), int(row[2]))


MigrationFaultHook = Callable[[str], None]
