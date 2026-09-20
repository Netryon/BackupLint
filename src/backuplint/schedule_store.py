"""SQLite-backed local scheduler history (no secrets)."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from backuplint import __version__ as APPLICATION_VERSION
from backuplint.schedule_config import ScheduleCheckType
from backuplint.sqlite_backup import (
    SCHEDULE_NOTES,
    BackupManifest,
    BackupVerification,
    PersistenceDiagnostics,
    backup_sqlite_database,
    diagnose_sqlite_file,
    verify_sqlite_backup,
    wal_checkpoint,
)
from backuplint.sqlite_util import configure_store_connection, immediate_transaction

SCHEDULE_STORE_SCHEMA_VERSION = 1

# Ordered migration targets for future schedule schema bumps.
_SCHEDULE_MIGRATIONS: tuple[tuple[int, str], ...] = ()


class ScheduleStoreError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class ScheduleRunRecord:
    id: int
    check_type: ScheduleCheckType
    started_at: datetime
    finished_at: datetime
    result: str
    exit_code: int
    duration_seconds: float
    detail: str | None = None


@dataclass(frozen=True)
class ScheduleJobState:
    check_type: ScheduleCheckType
    next_run: datetime | None
    last_started: datetime | None
    last_finished: datetime | None
    last_result: str | None
    last_exit_code: int | None
    last_success: datetime | None
    last_failure: datetime | None
    skipped_reason: str | None = None


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_dt(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return _aware(parsed)


class ScheduleStore:
    """Local schedule history and next-run bookkeeping.

    One ScheduleStore process is supported per database file. Thread access is
    serialized with an RLock; multi-process writers on the same file are
    unsupported.
    """

    def __init__(
        self,
        path: Path,
        *,
        history_limit: int = 500,
        migration_fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.path = path
        self.history_limit = history_limit
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create with restrictive perms.
        if not self.path.exists():
            self.path.touch(mode=0o600)
            self.path.chmod(0o600)
        self._lock = threading.RLock()
        self._migration_fault_hook = migration_fault_hook
        self._conn = sqlite3.connect(
            str(self.path),
            isolation_level=None,
            check_same_thread=False,
        )
        configure_store_connection(self._conn)
        try:
            self._init_schema()
        except BaseException:
            self._conn.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def schema_version(self) -> int:
        with self._lock:
            raw = self._meta_get("schema_version")
            return int(raw) if raw is not None else 1

    def backup(self, dest_path: Path) -> BackupManifest:
        with self._lock:
            return backup_sqlite_database(
                self._conn,
                Path(dest_path),
                store_type="schedule",
                schema_version=self.schema_version(),
                application_version=APPLICATION_VERSION,
                notes=SCHEDULE_NOTES,
            )

    def checkpoint(self, mode: str = "PASSIVE") -> tuple[int, int, int]:
        with self._lock:
            return wal_checkpoint(self._conn, mode=mode)

    def diagnostics(self) -> PersistenceDiagnostics:
        return diagnose_sqlite_file(
            self.path,
            store_type="schedule",
            max_supported_schema=SCHEDULE_STORE_SCHEMA_VERSION,
        )

    @staticmethod
    def verify_backup(path: Path) -> BackupVerification:
        return verify_sqlite_backup(
            Path(path),
            store_type="schedule",
            max_supported_schema=SCHEDULE_STORE_SCHEMA_VERSION,
        )

    def _init_schema(self) -> None:
        with self._lock:
            # executescript() issues COMMIT; keep schema bootstrap outside txn.
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  check_type TEXT NOT NULL,
                  started_at TEXT NOT NULL,
                  finished_at TEXT NOT NULL,
                  result TEXT NOT NULL,
                  exit_code INTEGER NOT NULL,
                  duration_seconds REAL NOT NULL,
                  detail TEXT
                );
                CREATE TABLE IF NOT EXISTS job_state (
                  check_type TEXT PRIMARY KEY,
                  next_run TEXT,
                  last_started TEXT,
                  last_finished TEXT,
                  last_result TEXT,
                  last_exit_code INTEGER,
                  last_success TEXT,
                  last_failure TEXT,
                  skipped_reason TEXT
                );
                """
            )
            with immediate_transaction(self._conn):
                self._migrate()

    def _meta_get(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def _meta_set(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )

    def _migration_fault(self, point: str) -> None:
        hook = self._migration_fault_hook
        if hook is not None:
            hook(point)

    def _migrate(self) -> None:
        self._migration_fault("before_migration")
        raw = self._meta_get("schema_version")
        if raw is None:
            # Existing pre-meta DBs are treated as version 1 (current schema).
            current = 1
        else:
            current = int(raw)
        if current > SCHEDULE_STORE_SCHEMA_VERSION:
            raise ScheduleStoreError(
                f"schedule store schema_version {current} is newer than "
                f"supported {SCHEDULE_STORE_SCHEMA_VERSION}"
            )
        for target, method_name in _SCHEDULE_MIGRATIONS:
            if current >= target:
                continue
            self._migration_fault(f"before_v{target}")
            getattr(self, method_name)()
            self._migration_fault(f"after_schema_v{target}")
            current = target
            self._meta_set("schema_version", str(current))
            self._migration_fault(f"after_version_v{target}")
        self._meta_set("schema_version", str(SCHEDULE_STORE_SCHEMA_VERSION))
        self._migration_fault("after_migration")

    def record_run(
        self,
        *,
        check_type: ScheduleCheckType,
        started_at: datetime,
        finished_at: datetime,
        result: str,
        exit_code: int,
        duration_seconds: float,
        detail: str | None = None,
        next_run: datetime | None,
    ) -> ScheduleRunRecord:
        started = _aware(started_at).isoformat()
        finished = _aware(finished_at).isoformat()
        with self._lock:
            with immediate_transaction(self._conn):
                cur = self._conn.execute(
                    """
                    INSERT INTO runs (
                      check_type, started_at, finished_at, result, exit_code,
                      duration_seconds, detail
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        check_type.value,
                        started,
                        finished,
                        result,
                        exit_code,
                        duration_seconds,
                        detail,
                    ),
                )
                run_id = int(cur.lastrowid)
                existing = self._fetch_job_state(check_type)
                if result == "PASS":
                    last_success_val: str | None = finished
                elif existing is not None and existing.last_success is not None:
                    last_success_val = existing.last_success.isoformat()
                else:
                    last_success_val = None
                if result in {"FAIL", "ERROR"}:
                    last_failure_val: str | None = finished
                elif existing is not None and existing.last_failure is not None:
                    last_failure_val = existing.last_failure.isoformat()
                else:
                    last_failure_val = None
                self._conn.execute(
                    """
                    INSERT INTO job_state (
                      check_type, next_run, last_started, last_finished, last_result,
                      last_exit_code, last_success, last_failure, skipped_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    ON CONFLICT(check_type) DO UPDATE SET
                      next_run=excluded.next_run,
                      last_started=excluded.last_started,
                      last_finished=excluded.last_finished,
                      last_result=excluded.last_result,
                      last_exit_code=excluded.last_exit_code,
                      last_success=excluded.last_success,
                      last_failure=excluded.last_failure,
                      skipped_reason=NULL
                    """,
                    (
                        check_type.value,
                        _aware(next_run).isoformat() if next_run is not None else None,
                        started,
                        finished,
                        result,
                        exit_code,
                        last_success_val,
                        last_failure_val,
                    ),
                )
                self._trim_history()
        return ScheduleRunRecord(
            id=run_id,
            check_type=check_type,
            started_at=_aware(started_at),
            finished_at=_aware(finished_at),
            result=result,
            exit_code=exit_code,
            duration_seconds=duration_seconds,
            detail=detail,
        )

    def set_next_run(self, check_type: ScheduleCheckType, next_run: datetime) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO job_state (check_type, next_run)
                VALUES (?, ?)
                ON CONFLICT(check_type) DO UPDATE SET next_run=excluded.next_run
                """,
                (check_type.value, _aware(next_run).isoformat()),
            )

    def set_skipped(self, check_type: ScheduleCheckType, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO job_state (check_type, skipped_reason)
                VALUES (?, ?)
                ON CONFLICT(check_type) DO UPDATE SET skipped_reason=excluded.skipped_reason
                """,
                (check_type.value, reason),
            )

    def _fetch_job_state(self, check_type: ScheduleCheckType) -> ScheduleJobState | None:
        row = self._conn.execute(
            """
            SELECT check_type, next_run, last_started, last_finished, last_result,
                   last_exit_code, last_success, last_failure, skipped_reason
            FROM job_state WHERE check_type = ?
            """,
            (check_type.value,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_job_state(row)

    def get_job_state(self, check_type: ScheduleCheckType) -> ScheduleJobState | None:
        with self._lock:
            return self._fetch_job_state(check_type)

    def list_job_states(self) -> list[ScheduleJobState]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT check_type, next_run, last_started, last_finished, last_result,
                       last_exit_code, last_success, last_failure, skipped_reason
                FROM job_state ORDER BY check_type
                """
            ).fetchall()
        return [_row_to_job_state(row) for row in rows]

    def history(self, *, limit: int = 50) -> list[ScheduleRunRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, check_type, started_at, finished_at, result, exit_code,
                       duration_seconds, detail
                FROM runs ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            ScheduleRunRecord(
                id=row[0],
                check_type=ScheduleCheckType(row[1]),
                started_at=_parse_dt(row[2]) or datetime.now(UTC),
                finished_at=_parse_dt(row[3]) or datetime.now(UTC),
                result=row[4],
                exit_code=row[5],
                duration_seconds=row[6],
                detail=row[7],
            )
            for row in rows
        ]

    def _trim_history(self) -> None:
        self._conn.execute(
            """
            DELETE FROM runs WHERE id NOT IN (
              SELECT id FROM runs ORDER BY id DESC LIMIT ?
            )
            """,
            (self.history_limit,),
        )


def _row_to_job_state(row: tuple[object, ...]) -> ScheduleJobState:
    return ScheduleJobState(
        check_type=ScheduleCheckType(str(row[0])),
        next_run=_parse_dt(None if row[1] is None else str(row[1])),
        last_started=_parse_dt(None if row[2] is None else str(row[2])),
        last_finished=_parse_dt(None if row[3] is None else str(row[3])),
        last_result=None if row[4] is None else str(row[4]),
        last_exit_code=None if row[5] is None else int(row[5]),  # type: ignore[arg-type]
        last_success=_parse_dt(None if row[6] is None else str(row[6])),
        last_failure=_parse_dt(None if row[7] is None else str(row[7])),
        skipped_reason=None if row[8] is None else str(row[8]),
    )
