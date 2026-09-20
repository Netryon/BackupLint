"""Controller SQLite persistence for BackupLint fleet."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from backuplint import __version__ as APPLICATION_VERSION
from backuplint.events import (
    EVENT_SCHEMA_VERSION,
    CanonicalEvent,
    EventType,
    event_from_audit_result,
    legacy_run_id,
    new_event_id,
    parse_event,
)
from backuplint.fleet.policy_store import PolicyStoreMixin
from backuplint.fleet.protocol import PROTOCOL_VERSION, ResultEnvelope, utc_now_iso
from backuplint.fleet.telemetry import make_store_lock
from backuplint.sqlite_backup import (
    CONTROLLER_NOTES,
    BackupManifest,
    BackupVerification,
    PersistenceDiagnostics,
    backup_sqlite_database,
    diagnose_sqlite_file,
    verify_sqlite_backup,
    wal_checkpoint,
)
from backuplint.sqlite_util import configure_store_connection, immediate_transaction

if TYPE_CHECKING:
    from backuplint.fleet.telemetry import ControllerTelemetry

STORE_SCHEMA_VERSION = 7

# Ordered migration targets: (target_version, method_name).
_CONTROLLER_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (2, "_migrate_to_v2"),
    (3, "_migrate_to_v3"),
    (4, "_migrate_to_v4"),
    (5, "_migrate_to_v5"),
    (6, "_migrate_to_v6"),
    (7, "_migrate_to_v7"),
)

# Stable agent registry statuses used by the current controller.
ALLOWED_AGENT_STATUSES = frozenset({"active", "revoked"})
ALLOWED_PENDING_STATUSES = frozenset(
    {"pending", "consumed", "expired", "revoked", "failed"}
)


class ControllerStoreError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _DuplicateSubmission(Exception):
    """Internal signal: duplicate submission_id inside an open transaction."""


@dataclass(frozen=True)
class AgentRecord:
    agent_id: str
    label: str
    hostname: str
    status: str
    first_seen: str
    last_seen: str | None
    protocol_version: int
    software_version: str | None = None
    capabilities_json: str | None = None
    capabilities_updated_at: str | None = None


class ControllerStore(PolicyStoreMixin):
    """SQLite-backed controller state.

    One ControllerStore process is supported per database file. Concurrent
    threads within that process are serialized via an RLock; opening a second
    controller process against the same file is unsupported.
    """

    _store_error_type = ControllerStoreError

    def __init__(
        self,
        path: Path,
        *,
        migration_fault_hook: Callable[[str], None] | None = None,
        telemetry: ControllerTelemetry | None = None,
    ) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch(mode=0o600)
            self.path.chmod(0o600)
        self._telemetry = telemetry
        self._lock = make_store_lock(telemetry)
        # Test-only migration fault injection hook: callable(point: str) -> None.
        self._migration_fault_hook = migration_fault_hook
        self._conn = sqlite3.connect(
            str(self.path),
            isolation_level=None,
            check_same_thread=False,
        )
        configure_store_connection(self._conn)
        try:
            self._init()
        except BaseException:
            self._conn.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def open_reader(self) -> sqlite3.Connection:
        """Open a read-only connection for dashboard/query traffic.

        Avoids holding the write RLock during large SELECTs so ingest/heartbeat
        remain responsive under concurrent dashboard load (WAL).
        """
        uri = f"file:{self.path.as_posix()}?mode=ro"
        conn = sqlite3.connect(
            uri,
            uri=True,
            isolation_level=None,
            check_same_thread=False,
        )
        configure_store_connection(conn)
        return conn

    def prune_operational_history(
        self,
        *,
        events_keep_days: int = 90,
        submissions_keep_days: int = 90,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Delete aged operational history while preserving current-state signals.

        Distinguishes:
        - operational history: aged ``events`` / ``submissions`` rows
        - audit history: ``policy_audit`` (never deleted here)
        - current state: agents, heartbeats, latest AUDIT_COMPLETED per agent

        Defaults are conservative (90 days). Set keep_days <= 0 to skip that table.
        """
        if events_keep_days < 0 or submissions_keep_days < 0:
            raise ControllerStoreError("keep_days must be >= 0")
        moment = now or datetime.now(UTC)
        events_cutoff = (moment - timedelta(days=events_keep_days)).isoformat()
        submissions_cutoff = (
            moment - timedelta(days=submissions_keep_days)
        ).isoformat()
        deleted_events = 0
        deleted_submissions = 0
        with self._lock:
            with immediate_transaction(self._conn):
                if events_keep_days > 0:
                    keep_rows = self._conn.execute(
                        """
                        SELECT agent_id, event_id FROM events e
                        WHERE e.event_type = ?
                          AND e.occurred_at = (
                            SELECT MAX(e2.occurred_at) FROM events e2
                            WHERE e2.agent_id = e.agent_id AND e2.event_type = ?
                          )
                        """,
                        (
                            EventType.AUDIT_COMPLETED.value,
                            EventType.AUDIT_COMPLETED.value,
                        ),
                    ).fetchall()
                    keep_ids = {str(r[1]) for r in keep_rows}
                    # Chunk deletes to avoid huge IN lists.
                    aged = self._conn.execute(
                        """
                        SELECT event_id FROM events
                        WHERE occurred_at < ?
                        """,
                        (events_cutoff,),
                    ).fetchall()
                    to_delete = [str(r[0]) for r in aged if str(r[0]) not in keep_ids]
                    for i in range(0, len(to_delete), 500):
                        chunk = to_delete[i : i + 500]
                        placeholders = ",".join("?" for _ in chunk)
                        self._conn.execute(
                            f"DELETE FROM events WHERE event_id IN ({placeholders})",  # noqa: S608  # nosec B608
                            chunk,
                        )
                        deleted_events += len(chunk)
                if submissions_keep_days > 0:
                    keep_rows = self._conn.execute(
                        """
                        SELECT agent_id, submission_id FROM submissions s
                        WHERE s.received_time = (
                          SELECT MAX(s2.received_time) FROM submissions s2
                          WHERE s2.agent_id = s.agent_id
                        )
                        """
                    ).fetchall()
                    keep_ids = {str(r[1]) for r in keep_rows}
                    aged = self._conn.execute(
                        """
                        SELECT submission_id FROM submissions
                        WHERE received_time < ?
                        """,
                        (submissions_cutoff,),
                    ).fetchall()
                    to_delete = [str(r[0]) for r in aged if str(r[0]) not in keep_ids]
                    for i in range(0, len(to_delete), 500):
                        chunk = to_delete[i : i + 500]
                        placeholders = ",".join("?" for _ in chunk)
                        self._conn.execute(
                            f"DELETE FROM submissions WHERE submission_id IN ({placeholders})",  # noqa: S608  # nosec B608
                            chunk,
                        )
                        deleted_submissions += len(chunk)
        return {
            "deleted_events": int(deleted_events),
            "deleted_submissions": int(deleted_submissions),
            "events_keep_days": events_keep_days,
            "submissions_keep_days": submissions_keep_days,
        }

    def schema_version(self) -> int:
        with self._lock:
            raw = self._meta_get("schema_version")
            return int(raw) if raw is not None else 1

    def backup(self, dest_path: Path) -> BackupManifest:
        """Online-safe consistent SQLite backup of this store (DB only)."""
        with self._lock:
            return backup_sqlite_database(
                self._conn,
                Path(dest_path),
                store_type="controller",
                schema_version=self.schema_version(),
                application_version=APPLICATION_VERSION,
                notes=CONTROLLER_NOTES,
            )

    def checkpoint(self, mode: str = "PASSIVE") -> tuple[int, int, int]:
        with self._lock:
            return wal_checkpoint(self._conn, mode=mode)

    def diagnostics(self) -> PersistenceDiagnostics:
        # Close is not required; diagnose opens a separate connection.
        return diagnose_sqlite_file(
            self.path,
            store_type="controller",
            max_supported_schema=STORE_SCHEMA_VERSION,
        )

    @staticmethod
    def verify_backup(path: Path) -> BackupVerification:
        return verify_sqlite_backup(
            Path(path),
            store_type="controller",
            max_supported_schema=STORE_SCHEMA_VERSION,
        )

    def _init(self) -> None:
        with self._lock:
            # executescript() issues its own COMMIT; keep it outside our txn helper.
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS enroll_tokens (
                  token_hash TEXT PRIMARY KEY,
                  label TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  redeemed_at TEXT,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agents (
                  agent_id TEXT PRIMARY KEY,
                  label TEXT NOT NULL,
                  hostname TEXT NOT NULL,
                  status TEXT NOT NULL,
                  first_seen TEXT NOT NULL,
                  last_seen TEXT,
                  protocol_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS submissions (
                  submission_id TEXT PRIMARY KEY,
                  agent_id TEXT NOT NULL,
                  scan_time TEXT NOT NULL,
                  received_time TEXT NOT NULL,
                  backuplint_version TEXT NOT NULL,
                  platform TEXT NOT NULL,
                  result_json TEXT NOT NULL,
                  protocol_version INTEGER NOT NULL,
                  run_id TEXT,
                  event_id TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                  event_id TEXT PRIMARY KEY,
                  schema_version INTEGER NOT NULL,
                  event_type TEXT NOT NULL,
                  agent_id TEXT NOT NULL,
                  run_id TEXT NOT NULL,
                  submission_id TEXT UNIQUE,
                  occurred_at TEXT NOT NULL,
                  received_at TEXT NOT NULL,
                  status TEXT NOT NULL,
                  payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS heartbeats (
                  agent_id TEXT PRIMARY KEY,
                  last_heartbeat TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_enrollments (
                  agent_id TEXT PRIMARY KEY,
                  token_hash TEXT NOT NULL UNIQUE,
                  label TEXT NOT NULL,
                  status TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  consumed_at TEXT,
                  cert_serial TEXT,
                  cert_fingerprint TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_agent_received
                  ON events(agent_id, received_at);
                CREATE INDEX IF NOT EXISTS idx_events_agent_occurred
                  ON events(agent_id, occurred_at, received_at, event_id);
                CREATE INDEX IF NOT EXISTS idx_submissions_agent_scan
                  ON submissions(agent_id, scan_time, received_time, submission_id);
                CREATE INDEX IF NOT EXISTS idx_events_run_id
                  ON events(run_id);
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
        # Called under lock + open transaction from _init.
        # Ordered, fail-closed, transactional where SQLite permits.
        self._migration_fault("before_migration")
        raw = self._meta_get("schema_version")
        current = int(raw) if raw is not None else 1
        if current > STORE_SCHEMA_VERSION:
            raise ControllerStoreError(
                f"controller store schema_version {current} is newer than "
                f"supported {STORE_SCHEMA_VERSION}"
            )
        for target, method_name in _CONTROLLER_MIGRATIONS:
            if current >= target:
                continue
            self._migration_fault(f"before_v{target}")
            getattr(self, method_name)()
            self._migration_fault(f"after_schema_v{target}")
            current = target
            self._meta_set("schema_version", str(current))
            self._migration_fault(f"after_version_v{target}")
        self._meta_set("schema_version", str(current))
        self._migration_fault("after_migration")

    def _migrate_to_v2(self) -> None:
        # Ensure new columns exist on older submissions tables.
        cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(submissions)").fetchall()
        }
        if "run_id" not in cols:
            self._conn.execute("ALTER TABLE submissions ADD COLUMN run_id TEXT")
        if "event_id" not in cols:
            self._conn.execute("ALTER TABLE submissions ADD COLUMN event_id TEXT")

        rows = self._conn.execute(
            """
            SELECT submission_id, agent_id, scan_time, received_time, result_json, run_id, event_id
            FROM submissions
            """
        ).fetchall()
        for (
            submission_id,
            agent_id,
            scan_time,
            received_time,
            result_json,
            run_id,
            event_id,
        ) in rows:
            existing = self._conn.execute(
                "SELECT 1 FROM events WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
            if existing is not None:
                continue
            try:
                result = json.loads(result_json)
            except json.JSONDecodeError:
                result = {"summary": {"result": "UNKNOWN"}, "raw": result_json}
            if not isinstance(result, dict):
                result = {"summary": {"result": "UNKNOWN"}}
            resolved_run = run_id or legacy_run_id(submission_id)
            resolved_event = event_id or new_event_id()
            event = event_from_audit_result(
                result,
                occurred_at=scan_time,
                run_id=resolved_run,
                agent_id=agent_id,
                submission_id=submission_id,
                received_at=received_time,
                event_type=EventType.AUDIT_COMPLETED,
                event_id=resolved_event,
            )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO events (
                  event_id, schema_version, event_type, agent_id, run_id,
                  submission_id, occurred_at, received_at, status, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.schema_version,
                    event.event_type,
                    agent_id,
                    event.run_id,
                    submission_id,
                    event.occurred_at,
                    received_time,
                    event.status,
                    json.dumps(event.payload, separators=(",", ":")),
                ),
            )
            self._conn.execute(
                """
                UPDATE submissions
                SET run_id = ?, event_id = ?
                WHERE submission_id = ? AND (run_id IS NULL OR event_id IS NULL)
                """,
                (event.run_id, event.event_id, submission_id),
            )

    def _migrate_to_v3(self) -> None:
        """Capability / software-version columns on agents (not audit truth)."""
        cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(agents)").fetchall()
        }
        if "software_version" not in cols:
            self._conn.execute("ALTER TABLE agents ADD COLUMN software_version TEXT")
        if "capabilities_json" not in cols:
            self._conn.execute("ALTER TABLE agents ADD COLUMN capabilities_json TEXT")
        if "capabilities_updated_at" not in cols:
            self._conn.execute(
                "ALTER TABLE agents ADD COLUMN capabilities_updated_at TEXT"
            )

    def _migrate_to_v4(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_enrollments (
              agent_id TEXT PRIMARY KEY,
              token_hash TEXT NOT NULL UNIQUE,
              label TEXT NOT NULL,
              status TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              created_at TEXT NOT NULL,
              consumed_at TEXT,
              cert_serial TEXT,
              cert_fingerprint TEXT
            )
            """
        )

    def _migrate_to_v5(self) -> None:
        """Dashboard read-path indexes (bounded history / filters)."""
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_events_type_occurred
              ON events(event_type, occurred_at DESC, received_at DESC)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_events_status_occurred
              ON events(status, occurred_at DESC)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_events_agent_type_occurred
              ON events(agent_id, event_type, occurred_at DESC, received_at DESC, event_id DESC)
            """
        )

    def _migrate_to_v6(self) -> None:
        # Unfiltered / time-range dashboard history ORDER BY occurred_at.
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_events_occurred
              ON events(occurred_at DESC, received_at DESC, event_id DESC)
            """
        )

    def _migrate_to_v7(self) -> None:
        """Policy snapshots, assignments, drift, rollouts, and audit (v0.8)."""
        # Use execute() (not executescript) so we stay inside the open migration txn.
        statements = (
            """
            CREATE TABLE IF NOT EXISTS policy_meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS policies (
              policy_id TEXT PRIMARY KEY,
              display_name TEXT NOT NULL,
              description TEXT NOT NULL,
              created_at TEXT NOT NULL,
              created_by TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS policy_revisions (
              revision_id TEXT PRIMARY KEY,
              policy_id TEXT NOT NULL REFERENCES policies(policy_id),
              schema_version INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              created_by TEXT NOT NULL,
              content_sha256 TEXT NOT NULL,
              display_name TEXT NOT NULL,
              description TEXT NOT NULL,
              settings_json TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_policy_revisions_policy
              ON policy_revisions(policy_id, created_at DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS policy_groups (
              group_id TEXT PRIMARY KEY,
              display_name TEXT NOT NULL,
              description TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS policy_group_members (
              group_id TEXT NOT NULL REFERENCES policy_groups(group_id),
              agent_id TEXT NOT NULL REFERENCES agents(agent_id),
              added_at TEXT NOT NULL,
              PRIMARY KEY (group_id, agent_id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_policy_group_members_agent
              ON policy_group_members(agent_id)
            """,
            """
            CREATE TABLE IF NOT EXISTS policy_assignments (
              assignment_id TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              target_id TEXT NOT NULL DEFAULT '',
              policy_id TEXT NOT NULL,
              revision_id TEXT NOT NULL REFERENCES policy_revisions(revision_id),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(kind, target_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS policy_desired_state (
              agent_id TEXT PRIMARY KEY REFERENCES agents(agent_id),
              assignment_generation INTEGER NOT NULL,
              policy_id TEXT NOT NULL,
              revision_id TEXT NOT NULL,
              content_sha256 TEXT NOT NULL,
              resolved_at TEXT NOT NULL,
              source_kind TEXT NOT NULL,
              source_ref TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS policy_applied_state (
              agent_id TEXT PRIMARY KEY REFERENCES agents(agent_id),
              assignment_generation INTEGER NOT NULL,
              revision_id TEXT NOT NULL,
              content_sha256 TEXT NOT NULL,
              apply_status TEXT NOT NULL,
              drift_status TEXT NOT NULL,
              reason TEXT NOT NULL,
              applied_at TEXT NOT NULL,
              reported_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS policy_drift (
              agent_id TEXT PRIMARY KEY REFERENCES agents(agent_id),
              drift_status TEXT NOT NULL,
              desired_generation INTEGER,
              desired_revision_id TEXT,
              desired_sha256 TEXT,
              applied_revision_id TEXT,
              applied_sha256 TEXT,
              local_config_sha256 TEXT,
              reason TEXT,
              updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS policy_rollouts (
              rollout_id TEXT PRIMARY KEY,
              policy_id TEXT NOT NULL,
              revision_id TEXT NOT NULL,
              target_group_id TEXT,
              batch_size INTEGER NOT NULL,
              max_concurrent INTEGER NOT NULL,
              pause_between_batches_seconds INTEGER NOT NULL,
              failure_threshold INTEGER NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              started_at TEXT,
              completed_at TEXT,
              paused_at TEXT,
              created_by TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS policy_rollout_members (
              rollout_id TEXT NOT NULL REFERENCES policy_rollouts(rollout_id),
              agent_id TEXT NOT NULL,
              status TEXT NOT NULL,
              batch_number INTEGER NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (rollout_id, agent_id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_policy_rollout_members_status
              ON policy_rollout_members(rollout_id, status, batch_number)
            """,
            """
            CREATE TABLE IF NOT EXISTS policy_audit (
              audit_id TEXT PRIMARY KEY,
              occurred_at TEXT NOT NULL,
              actor TEXT NOT NULL,
              action TEXT NOT NULL,
              target TEXT NOT NULL,
              old_ref TEXT,
              new_ref TEXT,
              result TEXT NOT NULL,
              details_json TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_policy_audit_occurred
              ON policy_audit(occurred_at DESC)
            """,
        )
        for statement in statements:
            self._conn.execute(statement)
        self._conn.execute(
            """
            INSERT INTO policy_meta (key, value) VALUES ('assignment_generation', '0')
            ON CONFLICT(key) DO NOTHING
            """
        )

    def create_pending_enrollment(
        self,
        *,
        label: str,
        ttl: timedelta,
        agent_id: str | None = None,
    ) -> tuple[str, str]:
        """Create pending agent + one-time token. Returns (agent_id, raw_token).

        Persists only the token hash. Raw token is returned once to the caller.
        """
        from backuplint.fleet.protocol import (
            MAX_ENROLL_LABEL_CHARS,
            is_valid_agent_id,
            new_agent_id,
        )

        if not isinstance(label, str) or not label.strip():
            raise ControllerStoreError("label must be non-empty")
        if len(label.strip()) > MAX_ENROLL_LABEL_CHARS:
            raise ControllerStoreError("label too long")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in label):
            raise ControllerStoreError("label contains control characters")
        if agent_id is not None and not is_valid_agent_id(agent_id):
            raise ControllerStoreError("invalid agent_id")
        resolved_id = agent_id or new_agent_id()
        token = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)
        now = datetime.now(UTC)
        expires = now + ttl
        with self._lock:
            with immediate_transaction(self._conn):
                existing = self._conn.execute(
                    "SELECT 1 FROM agents WHERE agent_id = ?",
                    (resolved_id,),
                ).fetchone()
                if existing is not None:
                    raise ControllerStoreError(
                        f"agent_id already registered: {resolved_id}"
                    )
                pending = self._conn.execute(
                    "SELECT status FROM pending_enrollments WHERE agent_id = ?",
                    (resolved_id,),
                ).fetchone()
                if pending is not None and pending[0] == "pending":
                    raise ControllerStoreError(
                        f"pending enrollment already exists for {resolved_id}"
                    )
                self._conn.execute(
                    """
                    INSERT INTO pending_enrollments (
                      agent_id, token_hash, label, status, expires_at, created_at
                    ) VALUES (?, ?, ?, 'pending', ?, ?)
                    ON CONFLICT(agent_id) DO UPDATE SET
                      token_hash=excluded.token_hash,
                      label=excluded.label,
                      status='pending',
                      expires_at=excluded.expires_at,
                      created_at=excluded.created_at,
                      consumed_at=NULL,
                      cert_serial=NULL,
                      cert_fingerprint=NULL
                    """,
                    (
                        resolved_id,
                        token_hash,
                        label,
                        expires.isoformat(),
                        now.isoformat(),
                    ),
                )
                self._conn.execute(
                    """
                    INSERT INTO enroll_tokens (token_hash, label, expires_at, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (token_hash, label, expires.isoformat(), now.isoformat()),
                )
        return resolved_id, token

    def create_enroll_token(self, *, label: str, ttl: timedelta) -> str:
        """Compatibility wrapper: create pending enrollment; return raw token only."""
        _agent_id, token = self.create_pending_enrollment(label=label, ttl=ttl)
        return token

    def get_pending_enrollment(self, agent_id: str) -> dict[str, object] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT agent_id, label, status, expires_at, created_at, consumed_at,
                       cert_serial, cert_fingerprint
                FROM pending_enrollments WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "agent_id": row[0],
            "label": row[1],
            "status": row[2],
            "expires_at": row[3],
            "created_at": row[4],
            "consumed_at": row[5],
            "cert_serial": row[6],
            "cert_fingerprint": row[7],
        }

    def lookup_pending_by_token(self, token: str) -> dict[str, object] | None:
        token_hash = _hash_token(token)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT agent_id, label, status, expires_at
                FROM pending_enrollments WHERE token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
        if row is None:
            return None
        return {
            "agent_id": row[0],
            "label": row[1],
            "status": row[2],
            "expires_at": row[3],
        }

    def assert_enroll_token_usable(self, *, token: str, agent_id: str) -> None:
        """Cheap pre-signing check. Does not consume the token.

        Rejects unknown, mismatched, expired, revoked, or already-consumed tokens
        before expensive CSR signing. Final single-use consume remains in
        :meth:`complete_csr_enrollment` under the same atomic transaction.
        """
        token_hash = _hash_token(token)
        now = datetime.now(UTC)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT label, status, expires_at, token_hash
                FROM pending_enrollments WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
            if row is None:
                # Also reject tokens that exist for a different agent_id.
                other = self._conn.execute(
                    """
                    SELECT agent_id FROM pending_enrollments WHERE token_hash = ?
                    """,
                    (token_hash,),
                ).fetchone()
                if other is not None:
                    raise ControllerStoreError(
                        "enrollment token does not match pending agent"
                    )
                raise ControllerStoreError("unknown pending agent")
            _label, status, expires_at, stored_hash = row
            if stored_hash != token_hash:
                raise ControllerStoreError(
                    "enrollment token does not match pending agent"
                )
            if status == "consumed":
                raise ControllerStoreError("enrollment token already used")
            if status == "revoked":
                raise ControllerStoreError("pending enrollment revoked")
            if status == "expired":
                raise ControllerStoreError("enrollment token expired")
            if status != "pending":
                raise ControllerStoreError(f"pending enrollment is {status}")
            if datetime.fromisoformat(expires_at) < now:
                self._conn.execute(
                    """
                    UPDATE pending_enrollments SET status = 'expired'
                    WHERE agent_id = ? AND status = 'pending'
                    """,
                    (agent_id,),
                )
                raise ControllerStoreError("enrollment token expired")

    def complete_csr_enrollment(
        self,
        *,
        token: str,
        agent_id: str,
        hostname: str,
        cert_serial: str,
        cert_fingerprint: str,
    ) -> str:
        """Atomically consume pending token and register the agent."""
        token_hash = _hash_token(token)
        now = datetime.now(UTC)
        with self._lock:
            with immediate_transaction(self._conn):
                row = self._conn.execute(
                    """
                    SELECT label, status, expires_at, token_hash
                    FROM pending_enrollments WHERE agent_id = ?
                    """,
                    (agent_id,),
                ).fetchone()
                if row is None:
                    raise ControllerStoreError("unknown pending agent")
                label, status, expires_at, stored_hash = row
                if stored_hash != token_hash:
                    raise ControllerStoreError(
                        "enrollment token does not match pending agent"
                    )
                if status == "consumed":
                    raise ControllerStoreError("enrollment token already used")
                if status == "revoked":
                    raise ControllerStoreError("pending enrollment revoked")
                if status != "pending":
                    raise ControllerStoreError(f"pending enrollment is {status}")
                if datetime.fromisoformat(expires_at) < now:
                    self._conn.execute(
                        """
                        UPDATE pending_enrollments SET status = 'expired'
                        WHERE agent_id = ? AND status = 'pending'
                        """,
                        (agent_id,),
                    )
                    raise ControllerStoreError("enrollment token expired")
                cur = self._conn.execute(
                    """
                    UPDATE pending_enrollments
                    SET status = 'consumed',
                        consumed_at = ?,
                        cert_serial = ?,
                        cert_fingerprint = ?
                    WHERE agent_id = ? AND status = 'pending' AND token_hash = ?
                    """,
                    (
                        now.isoformat(),
                        cert_serial,
                        cert_fingerprint,
                        agent_id,
                        token_hash,
                    ),
                )
                if cur.rowcount != 1:
                    raise ControllerStoreError("enrollment token already used")
                self._conn.execute(
                    """
                    UPDATE enroll_tokens SET redeemed_at = ?
                    WHERE token_hash = ? AND redeemed_at IS NULL
                    """,
                    (now.isoformat(), token_hash),
                )
                existing = self._conn.execute(
                    "SELECT 1 FROM agents WHERE agent_id = ?",
                    (agent_id,),
                ).fetchone()
                if existing is not None:
                    raise ControllerStoreError("agent already registered")
                self._conn.execute(
                    """
                    INSERT INTO agents (
                      agent_id, label, hostname, status, first_seen, last_seen,
                      protocol_version
                    ) VALUES (?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        agent_id,
                        label,
                        hostname,
                        now.isoformat(),
                        now.isoformat(),
                        PROTOCOL_VERSION,
                    ),
                )
                return str(label)

    def list_pending_enrollments(
        self, *, status: str | None = "pending"
    ) -> list[dict[str, object]]:
        with self._lock:
            if status is None:
                rows = self._conn.execute(
                    """
                    SELECT agent_id, label, status, expires_at, created_at, consumed_at,
                           cert_serial, cert_fingerprint
                    FROM pending_enrollments ORDER BY created_at
                    """
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT agent_id, label, status, expires_at, created_at, consumed_at,
                           cert_serial, cert_fingerprint
                    FROM pending_enrollments WHERE status = ? ORDER BY created_at
                    """,
                    (status,),
                ).fetchall()
        return [
            {
                "agent_id": r[0],
                "label": r[1],
                "status": r[2],
                "expires_at": r[3],
                "created_at": r[4],
                "consumed_at": r[5],
                "cert_serial": r[6],
                "cert_fingerprint": r[7],
            }
            for r in rows
        ]

    def revoke_pending_enrollment(self, agent_id: str) -> None:
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE pending_enrollments SET status = 'revoked'
                WHERE agent_id = ? AND status = 'pending'
                """,
                (agent_id,),
            )
            if cur.rowcount != 1:
                raise ControllerStoreError(
                    f"no pending enrollment to revoke for {agent_id}"
                )

    def expire_due_pending_enrollments(self) -> int:
        now = datetime.now(UTC).isoformat()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE pending_enrollments SET status = 'expired'
                WHERE status = 'pending' AND expires_at < ?
                """,
                (now,),
            )
            return int(cur.rowcount)

    def redeem_enroll_token(self, token: str) -> str:
        """Return label if token valid; mark redeemed. Raises on failure."""
        token_hash = _hash_token(token)
        with self._lock:
            # Single-row redeem stays transactional for consistent reads.
            # Full redeem→cert→register remains a higher-layer enrollment concern.
            with immediate_transaction(self._conn):
                row = self._conn.execute(
                    """
                    SELECT label, expires_at, redeemed_at FROM enroll_tokens
                    WHERE token_hash = ?
                    """,
                    (token_hash,),
                ).fetchone()
                if row is None:
                    raise ControllerStoreError("invalid enrollment token")
                label, expires_at, redeemed_at = row
                if redeemed_at is not None:
                    raise ControllerStoreError("enrollment token already used")
                if datetime.fromisoformat(expires_at) < datetime.now(UTC):
                    raise ControllerStoreError("enrollment token expired")
                cur = self._conn.execute(
                    """
                    UPDATE enroll_tokens SET redeemed_at = ?
                    WHERE token_hash = ? AND redeemed_at IS NULL
                    """,
                    (utc_now_iso(), token_hash),
                )
                if cur.rowcount != 1:
                    raise ControllerStoreError("enrollment token already used")
                return label

    def register_agent(
        self,
        *,
        agent_id: str,
        label: str,
        hostname: str,
    ) -> AgentRecord:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO agents (
                  agent_id, label, hostname, status, first_seen, last_seen, protocol_version
                ) VALUES (?, ?, ?, 'active', ?, ?, ?)
                """,
                (agent_id, label, hostname, now, now, PROTOCOL_VERSION),
            )
        return AgentRecord(
            agent_id=agent_id,
            label=label,
            hostname=hostname,
            status="active",
            first_seen=now,
            last_seen=now,
            protocol_version=PROTOCOL_VERSION,
        )

    def _fetch_agent(self, agent_id: str) -> AgentRecord | None:
        row = self._conn.execute(
            """
            SELECT agent_id, label, hostname, status, first_seen, last_seen,
                   protocol_version, software_version, capabilities_json,
                   capabilities_updated_at
            FROM agents WHERE agent_id = ?
            """,
            (agent_id,),
        ).fetchone()
        if row is None:
            return None
        return AgentRecord(*row)

    def get_agent(self, agent_id: str) -> AgentRecord | None:
        with self._lock:
            return self._fetch_agent(agent_id)

    def list_agents(self) -> list[AgentRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT agent_id, label, hostname, status, first_seen, last_seen,
                       protocol_version, software_version, capabilities_json,
                       capabilities_updated_at
                FROM agents ORDER BY agent_id
                """
            ).fetchall()
        return [AgentRecord(*row) for row in rows]

    def set_agent_status(self, agent_id: str, status: str) -> None:
        if status not in ALLOWED_AGENT_STATUSES:
            raise ControllerStoreError(
                f"invalid agent status {status!r}; "
                f"allowed={sorted(ALLOWED_AGENT_STATUSES)}"
            )
        with self._lock:
            cur = self._conn.execute(
                "UPDATE agents SET status = ? WHERE agent_id = ?",
                (status, agent_id),
            )
            if cur.rowcount != 1:
                raise ControllerStoreError(f"unknown agent: {agent_id}")

    def set_agent_label(self, agent_id: str, label: str) -> None:
        """Update display label without changing stable agent_id."""
        if not label.strip():
            raise ControllerStoreError("label must be non-empty")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE agents SET label = ? WHERE agent_id = ?",
                (label.strip(), agent_id),
            )
            if cur.rowcount != 1:
                raise ControllerStoreError(f"unknown agent: {agent_id}")

    def set_agent_hostname(self, agent_id: str, hostname: str) -> None:
        """Update reported hostname without changing stable agent_id."""
        if not hostname.strip():
            raise ControllerStoreError("hostname must be non-empty")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE agents SET hostname = ? WHERE agent_id = ?",
                (hostname.strip(), agent_id),
            )
            if cur.rowcount != 1:
                raise ControllerStoreError(f"unknown agent: {agent_id}")

    def heartbeat(
        self,
        agent_id: str,
        *,
        protocol_version: int | None = None,
        software_version: str | None = None,
        capabilities: dict[str, object] | None = None,
    ) -> None:
        started = time.perf_counter()
        try:
            with self._lock:
                with immediate_transaction(self._conn):
                    agent = self._fetch_agent(agent_id)
                    if agent is None:
                        raise ControllerStoreError("unknown agent")
                    if agent.status != "active":
                        raise ControllerStoreError(f"agent is {agent.status}")
                    now = utc_now_iso()
                    self._conn.execute(
                        """
                        INSERT INTO heartbeats (agent_id, last_heartbeat) VALUES (?, ?)
                        ON CONFLICT(agent_id) DO UPDATE SET last_heartbeat=excluded.last_heartbeat
                        """,
                        (agent_id, now),
                    )
                    caps_json: str | None = None
                    if capabilities is not None:
                        from backuplint.fleet.capabilities import (
                            capabilities_to_json,
                            validate_capabilities_document,
                        )

                        caps_json = capabilities_to_json(
                            validate_capabilities_document(capabilities)
                        )
                    self._conn.execute(
                        """
                        UPDATE agents SET
                          last_seen = ?,
                          protocol_version = COALESCE(?, protocol_version),
                          software_version = COALESCE(?, software_version),
                          capabilities_json = COALESCE(?, capabilities_json),
                          capabilities_updated_at = CASE
                            WHEN ? IS NOT NULL THEN ?
                            ELSE capabilities_updated_at
                          END
                        WHERE agent_id = ?
                        """,
                        (
                            now,
                            protocol_version,
                            software_version,
                            caps_json,
                            caps_json,
                            now,
                            agent_id,
                        ),
                    )
        finally:
            if self._telemetry is not None:
                self._telemetry.record_db_op(
                    "heartbeat", (time.perf_counter() - started) * 1000.0
                )

    def get_agent_capabilities(self, agent_id: str) -> dict[str, object] | None:
        """Return stored capabilities without touching audit/result truth."""
        agent = self.get_agent(agent_id)
        if agent is None or not agent.capabilities_json:
            return None
        try:
            raw = json.loads(agent.capabilities_json)
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None
        return raw

    def ingest_result(self, envelope: ResultEnvelope) -> bool:
        """Return True if newly stored, False if duplicate submission_id."""
        started = time.perf_counter()
        try:
            try:
                return self._ingest_result_atomic(envelope)
            except _DuplicateSubmission:
                return False
        finally:
            if self._telemetry is not None:
                self._telemetry.record_db_op(
                    "ingest", (time.perf_counter() - started) * 1000.0
                )

    def _ingest_result_atomic(self, envelope: ResultEnvelope) -> bool:
        with self._lock:
            with immediate_transaction(self._conn):
                agent = self._fetch_agent(envelope.agent_id)
                if agent is None:
                    raise ControllerStoreError("unknown agent")
                if agent.status != "active":
                    raise ControllerStoreError(f"agent is {agent.status}")
                received = utc_now_iso()
                run_id = envelope.run_id or legacy_run_id(envelope.submission_id)
                event = event_from_audit_result(
                    envelope.result,
                    occurred_at=envelope.scan_time,
                    run_id=run_id,
                    agent_id=envelope.agent_id,
                    submission_id=envelope.submission_id,
                    received_at=received,
                    event_type=EventType.AUDIT_COMPLETED,
                )
                try:
                    self._conn.execute(
                        """
                        INSERT INTO submissions (
                          submission_id, agent_id, scan_time, received_time,
                          backuplint_version, platform, result_json, protocol_version,
                          run_id, event_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            envelope.submission_id,
                            envelope.agent_id,
                            envelope.scan_time,
                            received,
                            envelope.backuplint_version,
                            envelope.platform,
                            json.dumps(envelope.result, separators=(",", ":")),
                            envelope.protocol_version,
                            event.run_id,
                            event.event_id,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise _DuplicateSubmission from exc
                self._conn.execute(
                    """
                    INSERT INTO events (
                      event_id, schema_version, event_type, agent_id, run_id,
                      submission_id, occurred_at, received_at, status, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        EVENT_SCHEMA_VERSION,
                        event.event_type,
                        envelope.agent_id,
                        event.run_id,
                        envelope.submission_id,
                        event.occurred_at,
                        received,
                        event.status,
                        json.dumps(event.payload, separators=(",", ":")),
                    ),
                )
                self._conn.execute(
                    "UPDATE agents SET last_seen = ? WHERE agent_id = ?",
                    (received, envelope.agent_id),
                )
                return True

    def latest_result(self, agent_id: str) -> dict[str, object] | None:
        """Most recently *ingested* submission (ORDER BY received_time).

        Operational/debug helper for ingest lag. For dashboard backup health,
        prefer :meth:`current_result_by_occurred_at`.
        """
        with self._lock:
            row = self._conn.execute(
                """
                SELECT submission_id, scan_time, received_time, backuplint_version,
                       platform, result_json, run_id, event_id
                FROM submissions WHERE agent_id = ?
                ORDER BY received_time DESC LIMIT 1
                """,
                (agent_id,),
            ).fetchone()
        return self._submission_row_to_dict(row)

    def current_result_by_occurred_at(self, agent_id: str) -> dict[str, object] | None:
        """Current backup result by scan_time (occurred_at), not receive order.

        An older scan that arrives later must not replace a newer scan for
        health/dashboard semantics. Tie-break: received_time, submission_id.
        """
        with self._lock:
            row = self._conn.execute(
                """
                SELECT submission_id, scan_time, received_time, backuplint_version,
                       platform, result_json, run_id, event_id
                FROM submissions WHERE agent_id = ?
                ORDER BY scan_time DESC, received_time DESC, submission_id DESC
                LIMIT 1
                """,
                (agent_id,),
            ).fetchone()
        return self._submission_row_to_dict(row)

    def event_for_submission(self, submission_id: str) -> CanonicalEvent | None:
        """Return the stored canonical event for a submission, if present."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT event_id, schema_version, event_type, agent_id, run_id,
                       submission_id, occurred_at, received_at, status, payload_json
                FROM events WHERE submission_id = ?
                """,
                (submission_id,),
            ).fetchone()
        data = self._event_row_to_dict(row)
        if data is None:
            return None
        return parse_event(
            {
                "schema_version": data["schema_version"],
                "event_id": data["event_id"],
                "event_type": data["event_type"],
                "run_id": data["run_id"],
                "occurred_at": data["occurred_at"],
                "status": data["status"],
                "payload": data["payload"],
                "agent_id": data.get("agent_id"),
                "received_at": data.get("received_at"),
                "submission_id": data.get("submission_id"),
            }
        )

    def latest_event(self, agent_id: str) -> dict[str, object] | None:
        """Most recently *ingested* event (ORDER BY received_at).

        Last-received / transport-order helper. For audit current state,
        prefer :meth:`current_event_by_occurred_at`.
        """
        with self._lock:
            row = self._conn.execute(
                """
                SELECT event_id, schema_version, event_type, agent_id, run_id,
                       submission_id, occurred_at, received_at, status, payload_json
                FROM events WHERE agent_id = ?
                ORDER BY received_at DESC LIMIT 1
                """,
                (agent_id,),
            ).fetchone()
        return self._event_row_to_dict(row)

    def current_event_by_occurred_at(self, agent_id: str) -> dict[str, object] | None:
        """Current ``audit.completed`` event by occurred_at (scan time).

        Invariant: among events for ``agent_id`` with
        ``event_type = audit.completed``, pick newest by
        ``occurred_at``, then ``received_at``, then ``event_id``.
        Older events that arrive later do not overwrite newer current state.
        Heartbeat / online status is separate (:meth:`last_heartbeat`).
        """
        with self._lock:
            row = self._conn.execute(
                """
                SELECT event_id, schema_version, event_type, agent_id, run_id,
                       submission_id, occurred_at, received_at, status, payload_json
                FROM events
                WHERE agent_id = ? AND event_type = ?
                ORDER BY occurred_at DESC, received_at DESC, event_id DESC
                LIMIT 1
                """,
                (agent_id, EventType.AUDIT_COMPLETED.value),
            ).fetchone()
        return self._event_row_to_dict(row)

    @staticmethod
    def _submission_row_to_dict(
        row: tuple[object, ...] | None,
    ) -> dict[str, object] | None:
        if row is None:
            return None
        return {
            "submission_id": row[0],
            "scan_time": row[1],
            "received_time": row[2],
            "backuplint_version": row[3],
            "platform": row[4],
            "result": json.loads(str(row[5])),
            "run_id": row[6],
            "event_id": row[7],
        }

    @staticmethod
    def _event_row_to_dict(row: tuple[object, ...] | None) -> dict[str, object] | None:
        if row is None:
            return None
        return {
            "event_id": row[0],
            "schema_version": row[1],
            "event_type": row[2],
            "agent_id": row[3],
            "run_id": row[4],
            "submission_id": row[5],
            "occurred_at": row[6],
            "received_at": row[7],
            "status": row[8],
            "payload": json.loads(str(row[9])),
        }

    def last_heartbeat(self, agent_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_heartbeat FROM heartbeats WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
        return row[0] if row else None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
