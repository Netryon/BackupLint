"""Durable SQLite-backed SIEM export queue."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from backuplint.events import assert_payload_safe
from backuplint.siem.event import (
    NON_EXPORTABLE_FAMILIES,
    SIEM_EVENT_SCHEMA_VERSION,
    SiemEvent,
    SiemEventFamily,
    SiemSeverity,
    deterministic_event_id,
)
from backuplint.sqlite_util import configure_store_connection, immediate_transaction

SIEM_QUEUE_SCHEMA_VERSION = 1

_STATUS_PENDING = "pending"
_STATUS_IN_FLIGHT = "in_flight"
_STATUS_DELIVERED = "delivered"
_STATUS_DEAD_LETTER = "dead_letter"

_LAST_ERROR_MAX = 500


class SiemQueueError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class EnqueueResult(StrEnum):
    ENQUEUED = "enqueued"
    DUPLICATE = "duplicate"
    REJECTED_NON_EXPORTABLE = "rejected_non_exportable"
    REJECTED_HEALTHY_AT_CAPACITY = "rejected_healthy_at_capacity"
    OVERFLOW_HARD_CEILING = "overflow_hard_ceiling"


@dataclass(frozen=True)
class QueueRow:
    row_id: int
    event_id: str
    event_family: str
    severity: str
    priority_class: str
    schema_version: int
    occurred_at: str
    enqueued_at: str
    payload_json: str
    status: str
    attempt_count: int
    next_attempt_at: str
    last_attempt_at: str | None
    last_error: str | None
    delivered_at: str | None

    def parse_event(self) -> SiemEvent:
        data = json.loads(self.payload_json)
        from backuplint.siem.event import SiemCategory, SiemEvent

        return SiemEvent(
            schema_version=int(data["siem_event_schema_version"]),
            event_id=str(data["event_id"]),
            event_family=SiemEventFamily(str(data["event_family"])),
            severity=SiemSeverity(str(data["severity"])),
            category=SiemCategory(str(data["category"])),
            occurred_at=str(data["occurred_at"]),
            received_at=str(data["received_at"]),
            source_role=str(data["source_role"]),
            summary=str(data["summary"]),
            agent_id=data.get("agent_id"),
            run_id=data.get("run_id"),
            submission_id=data.get("submission_id"),
            check_type=data.get("check_type"),
            result_state=data.get("result_state"),
            structured_details=data.get("structured_details"),
            software_version=data.get("software_version"),
            protocol_version=data.get("protocol_version"),
        )


@dataclass(frozen=True)
class QueueLimits:
    max_items: int = 20_000
    soft_cap_ratio: float = 0.8
    reserved_critical: int = 5_000
    hard_ceiling_multiplier: float = 2.0
    delivered_retention_hours: int = 24
    dead_letter_retention_days: int = 30


def redact_queue_error(message: str | None) -> str | None:
    if message is None:
        return None
    text = message.replace("\r", " ").replace("\n", " ")
    for needle in ("Bearer ", "bearer ", "Authorization:", "authorization:"):
        idx = text.find(needle)
        if idx >= 0:
            text = text[: idx + len(needle)] + "***"
    if len(text) > _LAST_ERROR_MAX:
        return text[: _LAST_ERROR_MAX - 3] + "..."
    return text


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class SiemExportQueue:
    """Separate SQLite queue for SIEM export rows."""

    def __init__(
        self,
        path: Path,
        *,
        limits: QueueLimits | None = None,
        busy_timeout_ms: int = 2_000,
    ) -> None:
        self.path = path
        self.limits = limits or QueueLimits()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch(mode=0o600)
            self.path.chmod(0o600)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path),
            isolation_level=None,
            check_same_thread=False,
        )
        configure_store_connection(self._conn, busy_timeout_ms=busy_timeout_ms)
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
            row = self._conn.execute(
                "SELECT value FROM siem_queue_meta WHERE key = 'schema_version'"
            ).fetchone()
            return int(row[0]) if row else 1

    def enqueue(self, event: SiemEvent, *, now: str | None = None) -> EnqueueResult:
        if event.event_family in NON_EXPORTABLE_FAMILIES:
            raise SiemQueueError(
                f"cannot enqueue non-exportable family: {event.event_family.value}"
            )
        payload = event.to_json()
        current = now or _utc_now_iso()
        with self._lock, immediate_transaction(self._conn):
            existing = self._conn.execute(
                "SELECT status FROM siem_export_queue WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
            if existing is not None:
                return EnqueueResult.DUPLICATE

            pending_count = self._pending_count_unlocked()
            soft_cap = int(self.limits.max_items * self.limits.soft_cap_ratio)
            hard_ceiling = int(self.limits.max_items * self.limits.hard_ceiling_multiplier)

            if pending_count >= hard_ceiling:
                return EnqueueResult.OVERFLOW_HARD_CEILING

            if (
                event.priority_class == "healthy"
                and pending_count >= soft_cap
                and pending_count >= self.limits.max_items
            ):
                return EnqueueResult.REJECTED_HEALTHY_AT_CAPACITY

            self._conn.execute(
                """
                INSERT INTO siem_export_queue (
                    event_id, event_family, severity, priority_class, schema_version,
                    occurred_at, enqueued_at, payload_json, status, attempt_count,
                    next_attempt_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    event.event_id,
                    event.event_family.value,
                    event.severity.value,
                    event.priority_class,
                    event.schema_version,
                    event.occurred_at,
                    current,
                    payload,
                    _STATUS_PENDING,
                    current,
                ),
            )
            return EnqueueResult.ENQUEUED

    def claim_next(self, *, now: str | None = None) -> QueueRow | None:
        current = now or _utc_now_iso()
        with self._lock, immediate_transaction(self._conn):
            row = self._conn.execute(
                """
                SELECT id, event_id, event_family, severity, priority_class,
                       schema_version, occurred_at, enqueued_at, payload_json, status,
                       attempt_count, next_attempt_at, last_attempt_at, last_error,
                       delivered_at
                FROM siem_export_queue
                WHERE status = ? AND next_attempt_at <= ?
                ORDER BY
                    CASE priority_class WHEN 'critical' THEN 1 ELSE 0 END DESC,
                    id ASC
                LIMIT 1
                """,
                (_STATUS_PENDING, current),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                """
                UPDATE siem_export_queue
                SET status = ?, last_attempt_at = ?
                WHERE id = ?
                """,
                (_STATUS_IN_FLIGHT, current, row[0]),
            )
            return self._row_from_tuple(row, status=_STATUS_IN_FLIGHT)

    def mark_delivered(self, event_id: str, *, delivered_at: str | None = None) -> None:
        when = delivered_at or _utc_now_iso()
        with self._lock, immediate_transaction(self._conn):
            self._conn.execute(
                """
                UPDATE siem_export_queue
                SET status = ?, delivered_at = ?, last_error = NULL
                WHERE event_id = ?
                """,
                (_STATUS_DELIVERED, when, event_id),
            )

    def mark_retry(
        self,
        event_id: str,
        *,
        attempt_count: int,
        next_attempt_at: str,
        last_error: str | None,
    ) -> None:
        with self._lock, immediate_transaction(self._conn):
            self._conn.execute(
                """
                UPDATE siem_export_queue
                SET status = ?, attempt_count = ?, next_attempt_at = ?,
                    last_error = ?
                WHERE event_id = ?
                """,
                (
                    _STATUS_PENDING,
                    attempt_count,
                    next_attempt_at,
                    redact_queue_error(last_error),
                    event_id,
                ),
            )

    def mark_dead_letter(self, event_id: str, *, last_error: str | None) -> None:
        with self._lock, immediate_transaction(self._conn):
            self._conn.execute(
                """
                UPDATE siem_export_queue
                SET status = ?, last_error = ?
                WHERE event_id = ?
                """,
                (_STATUS_DEAD_LETTER, redact_queue_error(last_error), event_id),
            )

    def requeue_in_flight(self, *, stale_before: str) -> int:
        with self._lock, immediate_transaction(self._conn):
            cur = self._conn.execute(
                """
                UPDATE siem_export_queue
                SET status = ?
                WHERE status = ? AND last_attempt_at IS NOT NULL AND last_attempt_at < ?
                """,
                (_STATUS_PENDING, _STATUS_IN_FLIGHT, stale_before),
            )
            return int(cur.rowcount)

    def depth_by_status(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM siem_export_queue GROUP BY status"
            ).fetchall()
        counts = {status: 0 for status in (
            _STATUS_PENDING,
            _STATUS_IN_FLIGHT,
            _STATUS_DELIVERED,
            _STATUS_DEAD_LETTER,
        )}
        for status, count in rows:
            counts[str(status)] = int(count)
        return counts

    def depth_by_priority(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT priority_class, COUNT(*)
                FROM siem_export_queue
                WHERE status IN (?, ?, ?)
                GROUP BY priority_class
                """,
                (_STATUS_PENDING, _STATUS_IN_FLIGHT, _STATUS_DEAD_LETTER),
            ).fetchall()
        return {str(priority): int(count) for priority, count in rows}

    def pending_count(self) -> int:
        with self._lock:
            return self._pending_count_unlocked()

    def oldest_pending_age_seconds(self, *, now: datetime | None = None) -> float | None:
        current = now or datetime.now(UTC)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT MIN(enqueued_at) FROM siem_export_queue
                WHERE status IN (?, ?)
                """,
                (_STATUS_PENDING, _STATUS_IN_FLIGHT),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        text = str(row[0])
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            enqueued = datetime.fromisoformat(text)
        except ValueError:
            return None
        if enqueued.tzinfo is None:
            enqueued = enqueued.replace(tzinfo=UTC)
        return max(0.0, (current - enqueued.astimezone(UTC)).total_seconds())

    def coalesce_healthy_rows(self) -> int:
        """Merge consecutive healthy rows near capacity into one synthetic marker row."""
        with self._lock, immediate_transaction(self._conn):
            pending = self._pending_count_unlocked()
            soft_cap = int(self.limits.max_items * self.limits.soft_cap_ratio)
            if pending < soft_cap:
                return 0
            rows = self._conn.execute(
                """
                SELECT id, event_id, payload_json, occurred_at, enqueued_at
                FROM siem_export_queue
                WHERE status = ? AND priority_class = 'healthy'
                ORDER BY id ASC
                """,
                (_STATUS_PENDING,),
            ).fetchall()
            if len(rows) < 2:
                return 0

            first_id = rows[0][0]
            last_id = rows[-1][0]
            agent_ids: list[str] = []
            first_occurred = str(rows[0][3])
            last_occurred = str(rows[-1][3])
            for row in rows:
                payload = json.loads(str(row[2]))
                agent = payload.get("agent_id")
                if isinstance(agent, str) and agent not in agent_ids:
                    if len(agent_ids) < 64:
                        agent_ids.append(agent)

            coalesced_count = len(rows)
            synthetic_id = deterministic_event_id(
                "coalesced",
                "agent_online",
                first_occurred,
                last_occurred,
                str(first_id),
                str(last_id),
            )
            marker_payload = {
                "siem_event_schema_version": SIEM_EVENT_SCHEMA_VERSION,
                "event_id": synthetic_id,
                "event_family": SiemEventFamily.AGENT_ONLINE.value,
                "severity": SiemSeverity.LOW.value,
                "category": "fleet_transport",
                "occurred_at": last_occurred,
                "received_at": _utc_now_iso(),
                "source_role": "controller",
                "summary": f"Coalesced {coalesced_count} agent online events",
                "structured_details": {
                    "coalesced_count": coalesced_count,
                    "first_occurred_at": first_occurred,
                    "last_occurred_at": last_occurred,
                    "agent_ids": agent_ids,
                    "coalesced": True,
                },
            }
            assert_payload_safe(marker_payload["structured_details"])
            now = _utc_now_iso()
            row_ids = [row[0] for row in rows]
            placeholders = ",".join("?" for _ in row_ids)
            self._conn.execute(
                f"DELETE FROM siem_export_queue WHERE id IN ({placeholders})",  # nosec B608
                row_ids,
            )
            self._conn.execute(
                """
                INSERT INTO siem_export_queue (
                    event_id, event_family, severity, priority_class, schema_version,
                    occurred_at, enqueued_at, payload_json, status, attempt_count,
                    next_attempt_at
                ) VALUES (?, ?, ?, 'healthy', ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (
                    synthetic_id,
                    SiemEventFamily.AGENT_ONLINE.value,
                    SiemSeverity.LOW.value,
                    SIEM_EVENT_SCHEMA_VERSION,
                    last_occurred,
                    now,
                    json.dumps(marker_payload, separators=(",", ":"), sort_keys=True),
                    _STATUS_PENDING,
                    now,
                ),
            )
            return coalesced_count

    def purge_delivered(self, *, older_than: datetime | None = None) -> int:
        cutoff = older_than or (
            datetime.now(UTC)
            - timedelta(hours=self.limits.delivered_retention_hours)
        )
        with self._lock, immediate_transaction(self._conn):
            cur = self._conn.execute(
                """
                DELETE FROM siem_export_queue
                WHERE status = ? AND delivered_at IS NOT NULL AND delivered_at < ?
                """,
                (_STATUS_DELIVERED, cutoff.isoformat()),
            )
            return int(cur.rowcount)

    def purge_dead_letter(self, *, older_than: datetime | None = None) -> int:
        cutoff = older_than or (
            datetime.now(UTC) - timedelta(days=self.limits.dead_letter_retention_days)
        )
        with self._lock, immediate_transaction(self._conn):
            cur = self._conn.execute(
                """
                DELETE FROM siem_export_queue
                WHERE status = ? AND enqueued_at < ?
                """,
                (_STATUS_DEAD_LETTER, cutoff.isoformat()),
            )
            return int(cur.rowcount)

    def at_soft_cap(self) -> bool:
        return self.pending_count() >= int(self.limits.max_items * self.limits.soft_cap_ratio)

    def at_hard_ceiling(self) -> bool:
        return self.pending_count() >= int(
            self.limits.max_items * self.limits.hard_ceiling_multiplier
        )

    def get_row(self, event_id: str) -> QueueRow | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, event_id, event_family, severity, priority_class,
                       schema_version, occurred_at, enqueued_at, payload_json, status,
                       attempt_count, next_attempt_at, last_attempt_at, last_error,
                       delivered_at
                FROM siem_export_queue WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_from_tuple(row)

    def _pending_count_unlocked(self) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(*) FROM siem_export_queue
            WHERE status IN (?, ?, ?)
            """,
            (_STATUS_PENDING, _STATUS_IN_FLIGHT, _STATUS_DEAD_LETTER),
        ).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _row_from_tuple(row: tuple[Any, ...], *, status: str | None = None) -> QueueRow:
        return QueueRow(
            row_id=int(row[0]),
            event_id=str(row[1]),
            event_family=str(row[2]),
            severity=str(row[3]),
            priority_class=str(row[4]),
            schema_version=int(row[5]),
            occurred_at=str(row[6]),
            enqueued_at=str(row[7]),
            payload_json=str(row[8]),
            status=status or str(row[9]),
            attempt_count=int(row[10]),
            next_attempt_at=str(row[11]),
            last_attempt_at=str(row[12]) if row[12] is not None else None,
            last_error=str(row[13]) if row[13] is not None else None,
            delivered_at=str(row[14]) if row[14] is not None else None,
        )

    def _init_schema(self) -> None:
        with immediate_transaction(self._conn):
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS siem_queue_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            row = self._conn.execute(
                "SELECT value FROM siem_queue_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO siem_queue_meta (key, value) VALUES ('schema_version', ?)",
                    (str(SIEM_QUEUE_SCHEMA_VERSION),),
                )
                self._migrate_to_v1()
            else:
                version = int(row[0])
                if version > SIEM_QUEUE_SCHEMA_VERSION:
                    raise SiemQueueError(
                        f"unsupported siem queue schema_version: {version}"
                    )
                if version < SIEM_QUEUE_SCHEMA_VERSION:
                    for target in range(version + 1, SIEM_QUEUE_SCHEMA_VERSION + 1):
                        migrator = getattr(self, f"_migrate_to_v{target}", None)
                        if migrator is None:
                            raise SiemQueueError(
                                f"missing migration to siem queue v{target}"
                            )
                        migrator()
                    self._conn.execute(
                        """
                        UPDATE siem_queue_meta SET value = ?
                        WHERE key = 'schema_version'
                        """,
                        (str(SIEM_QUEUE_SCHEMA_VERSION),),
                    )

    def _migrate_to_v1(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS siem_export_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                event_family TEXT NOT NULL,
                severity TEXT NOT NULL,
                priority_class TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                occurred_at TEXT NOT NULL,
                enqueued_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                last_attempt_at TEXT,
                last_error TEXT,
                delivered_at TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_siem_queue_ready
            ON siem_export_queue(status, next_attempt_at)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_siem_queue_priority
            ON siem_export_queue(priority_class, id)
            """
        )
