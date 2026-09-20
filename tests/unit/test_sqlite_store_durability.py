"""Deterministic SQLite persistence hardening tests (Agent 4).

Focus: WAL/reopen, crash-style rollback, concurrent store API use, migrations,
and integrity_check on isolated temp databases. Does not exercise scheduler
policy or enrollment redesign.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backuplint.fleet.controller_store import (
    STORE_SCHEMA_VERSION,
    ControllerStore,
    ControllerStoreError,
)
from backuplint.fleet.protocol import ResultEnvelope, new_submission_id
from backuplint.schedule_config import ScheduleCheckType
from backuplint.schedule_store import (
    SCHEDULE_STORE_SCHEMA_VERSION,
    ScheduleStore,
    ScheduleStoreError,
)
from backuplint.sqlite_util import integrity_check_ok, journal_mode


def _envelope(agent_id: str, submission_id: str | None = None) -> ResultEnvelope:
    return ResultEnvelope(
        agent_id=agent_id,
        submission_id=submission_id or new_submission_id(),
        scan_time=datetime.now(UTC).isoformat(),
        backuplint_version="0.5.0.dev0",
        platform="test",
        result={"summary": {"result": "PASS"}},
        run_id="run-" + "b" * 32,
    )


def _flaky_conn(
    real: sqlite3.Connection, *, fail_when: Callable[[str], bool]
) -> sqlite3.Connection:
    """Proxy that raises when fail_when(sql) is true (Python 3.14+ execute is RO)."""

    class _Proxy:
        def execute(self, sql: object, parameters: object = ()) -> sqlite3.Cursor:
            text = " ".join(str(sql).split())
            if fail_when(text):
                raise RuntimeError("simulated crash mid-write")
            return real.execute(sql, parameters)  # type: ignore[arg-type]

        def __getattr__(self, name: str) -> object:
            return getattr(real, name)

    return _Proxy()  # type: ignore[return-value]


def test_controller_wal_and_integrity_after_reopen(tmp_path: Path) -> None:
    db = tmp_path / "controller.sqlite3"
    store = ControllerStore(db)
    try:
        assert journal_mode(store._conn) == "wal"
        store.register_agent(agent_id="agent-wal000001", label="a", hostname="h")
        assert store.ingest_result(_envelope("agent-wal000001")) is True
        assert integrity_check_ok(store._conn) is True
    finally:
        store.close()

    store2 = ControllerStore(db)
    try:
        assert journal_mode(store2._conn) == "wal"
        assert store2.get_agent("agent-wal000001") is not None
        assert store2.latest_result("agent-wal000001") is not None
        assert store2.latest_event("agent-wal000001") is not None
        assert integrity_check_ok(store2._conn) is True
    finally:
        store2.close()


def test_ingest_result_atomic_on_mid_write_failure(tmp_path: Path) -> None:
    """If event INSERT fails after submission INSERT, neither row remains."""
    store = ControllerStore(tmp_path / "c.sqlite3")
    try:
        store.register_agent(agent_id="agent-atomic0001", label="a", hostname="h")
        env = _envelope("agent-atomic0001")
        real = store._conn
        store._conn = _flaky_conn(
            real, fail_when=lambda text: text.startswith("INSERT INTO events")
        )
        with pytest.raises(RuntimeError, match="simulated crash"):
            store.ingest_result(env)
        store._conn = real

        assert store.latest_result("agent-atomic0001") is None
        assert store.latest_event("agent-atomic0001") is None
        row = store._conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE submission_id = ?",
            (env.submission_id,),
        ).fetchone()
        assert row is not None and row[0] == 0
        # Store remains usable after rollback.
        assert store.ingest_result(env) is True
        assert store.latest_event("agent-atomic0001") is not None
    finally:
        store.close()


def test_heartbeat_atomic_updates_last_seen(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "c.sqlite3")
    try:
        store.register_agent(agent_id="agent-hb00000001", label="a", hostname="h")
        before = store.get_agent("agent-hb00000001")
        assert before is not None
        store.heartbeat("agent-hb00000001")
        hb = store.last_heartbeat("agent-hb00000001")
        after = store.get_agent("agent-hb00000001")
        assert hb is not None
        assert after is not None
        assert after.last_seen == hb
    finally:
        store.close()


def test_uncommitted_controller_changes_do_not_survive_close(tmp_path: Path) -> None:
    db = tmp_path / "c.sqlite3"
    store = ControllerStore(db)
    try:
        store.register_agent(agent_id="agent-commit0001", label="a", hostname="h")
        store._conn.execute("BEGIN IMMEDIATE")
        store._conn.execute(
            """
            INSERT INTO agents (
              agent_id, label, hostname, status, first_seen, last_seen, protocol_version
            ) VALUES (?, 'x', 'y', 'active', ?, ?, 1)
            """,
            ("agent-ghost00001", datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
        )
        # Abrupt close without COMMIT — ghost agent must not persist.
        store._conn.close()
    except Exception:
        store.close()
        raise

    store2 = ControllerStore(db)
    try:
        assert store2.get_agent("agent-commit0001") is not None
        assert store2.get_agent("agent-ghost00001") is None
        assert integrity_check_ok(store2._conn) is True
    finally:
        store2.close()


def test_duplicate_submission_race_one_winner(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "c.sqlite3")
    try:
        store.register_agent(agent_id="agent-race000001", label="a", hostname="h")
        sub = new_submission_id()
        results: list[bool] = []
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait(timeout=5)
            results.append(store.ingest_result(_envelope("agent-race000001", sub)))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert len(results) == 8
        assert results.count(True) == 1
        assert results.count(False) == 7
        assert store.latest_result("agent-race000001") is not None
        count = store._conn.execute("SELECT COUNT(*) FROM submissions").fetchone()
        assert count is not None and count[0] == 1
        ev = store._conn.execute("SELECT COUNT(*) FROM events").fetchone()
        assert ev is not None and ev[0] == 1
    finally:
        store.close()


def test_heartbeat_and_ingest_interleave(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "c.sqlite3")
    try:
        store.register_agent(agent_id="agent-interleave1", label="a", hostname="h")
        errors: list[BaseException] = []

        def heartbeats() -> None:
            try:
                for _ in range(40):
                    store.heartbeat("agent-interleave1")
            except BaseException as exc:  # noqa: BLE001 - collect for assertion
                errors.append(exc)

        def submits() -> None:
            try:
                for _ in range(40):
                    store.ingest_result(_envelope("agent-interleave1"))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=heartbeats),
            threading.Thread(target=heartbeats),
            threading.Thread(target=submits),
            threading.Thread(target=submits),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert errors == []
        assert store.last_heartbeat("agent-interleave1") is not None
        assert store.latest_event("agent-interleave1") is not None
        assert integrity_check_ok(store._conn) is True
    finally:
        store.close()


def test_invalid_agent_status_rejected(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "c.sqlite3")
    try:
        store.register_agent(agent_id="agent-status0001", label="a", hostname="h")
        with pytest.raises(ControllerStoreError, match="invalid agent status"):
            store.set_agent_status("agent-status0001", "pending")
        store.set_agent_status("agent-status0001", "revoked")
        assert store.get_agent("agent-status0001") is not None
        assert store.get_agent("agent-status0001").status == "revoked"
    finally:
        store.close()


def test_controller_rejects_newer_schema(tmp_path: Path) -> None:
    db = tmp_path / "c.sqlite3"
    store = ControllerStore(db)
    store.close()
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE meta SET value = ? WHERE key = 'schema_version'",
        (str(STORE_SCHEMA_VERSION + 1),),
    )
    conn.commit()
    conn.close()
    with pytest.raises(ControllerStoreError, match="newer than supported"):
        ControllerStore(db)


def test_controller_migration_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "c.sqlite3"
    # Seed minimal v1-like DB without meta/events.
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE agents (
          agent_id TEXT PRIMARY KEY,
          label TEXT NOT NULL,
          hostname TEXT NOT NULL,
          status TEXT NOT NULL,
          first_seen TEXT NOT NULL,
          last_seen TEXT,
          protocol_version INTEGER NOT NULL
        );
        CREATE TABLE submissions (
          submission_id TEXT PRIMARY KEY,
          agent_id TEXT NOT NULL,
          scan_time TEXT NOT NULL,
          received_time TEXT NOT NULL,
          backuplint_version TEXT NOT NULL,
          platform TEXT NOT NULL,
          result_json TEXT NOT NULL,
          protocol_version INTEGER NOT NULL
        );
        CREATE TABLE enroll_tokens (
          token_hash TEXT PRIMARY KEY,
          label TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          redeemed_at TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE heartbeats (
          agent_id TEXT PRIMARY KEY,
          last_heartbeat TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO agents (
          agent_id, label, hostname, status, first_seen, last_seen, protocol_version
        ) VALUES ('agent-mig0000001', 'a', 'h', 'active', '2026-01-01T00:00:00+00:00',
                  '2026-01-01T00:00:00+00:00', 1)
        """
    )
    conn.execute(
        """
        INSERT INTO submissions (
          submission_id, agent_id, scan_time, received_time,
          backuplint_version, platform, result_json, protocol_version
        ) VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'agent-mig0000001',
                  '2026-01-01T01:00:00+00:00', '2026-01-01T01:00:01+00:00',
                  '0.5.0.dev0', 'linux', '{"summary":{"result":"PASS"}}', 1)
        """
    )
    conn.commit()
    conn.close()

    first = ControllerStore(db)
    try:
        assert first.latest_event("agent-mig0000001") is not None
        ver = first._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        assert ver is not None and int(ver[0]) == STORE_SCHEMA_VERSION
    finally:
        first.close()

    second = ControllerStore(db)
    try:
        assert second.latest_event("agent-mig0000001") is not None
        events = second._conn.execute("SELECT COUNT(*) FROM events").fetchone()
        assert events is not None and events[0] == 1
        assert integrity_check_ok(second._conn) is True
    finally:
        second.close()


def test_schedule_store_wal_reopen_and_integrity(tmp_path: Path) -> None:
    db = tmp_path / "schedule.sqlite3"
    now = datetime.now(UTC)
    store = ScheduleStore(db, history_limit=10)
    try:
        assert journal_mode(store._conn) == "wal"
        store.record_run(
            check_type=ScheduleCheckType.COVERAGE,
            started_at=now,
            finished_at=now,
            result="PASS",
            exit_code=0,
            duration_seconds=0.2,
            next_run=now + timedelta(minutes=15),
        )
        assert integrity_check_ok(store._conn) is True
    finally:
        store.close()

    store2 = ScheduleStore(db, history_limit=10)
    try:
        assert journal_mode(store2._conn) == "wal"
        state = store2.get_job_state(ScheduleCheckType.COVERAGE)
        assert state is not None
        assert state.last_result == "PASS"
        assert len(store2.history(limit=5)) == 1
        assert integrity_check_ok(store2._conn) is True
        ver = store2._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        assert ver is not None and int(ver[0]) == SCHEDULE_STORE_SCHEMA_VERSION
    finally:
        store2.close()


def test_schedule_record_run_atomic_on_failure(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "s.sqlite3", history_limit=50)
    try:
        now = datetime.now(UTC)
        real = store._conn
        store._conn = _flaky_conn(
            real,
            fail_when=lambda text: text.startswith("INSERT INTO job_state"),
        )
        with pytest.raises(RuntimeError, match="simulated crash"):
            store.record_run(
                check_type=ScheduleCheckType.COVERAGE,
                started_at=now,
                finished_at=now,
                result="PASS",
                exit_code=0,
                duration_seconds=0.1,
                next_run=now + timedelta(minutes=5),
            )
        store._conn = real

        assert store.get_job_state(ScheduleCheckType.COVERAGE) is None
        assert store.history(limit=10) == []
        # Recover and write successfully.
        store.record_run(
            check_type=ScheduleCheckType.COVERAGE,
            started_at=now,
            finished_at=now,
            result="FAIL",
            exit_code=1,
            duration_seconds=0.1,
            next_run=now + timedelta(minutes=5),
        )
        state = store.get_job_state(ScheduleCheckType.COVERAGE)
        assert state is not None
        assert state.last_result == "FAIL"
    finally:
        store.close()


def test_schedule_concurrent_record_and_read(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "s.sqlite3", history_limit=200)
    now = datetime.now(UTC)
    errors: list[BaseException] = []

    def writer(n: int) -> None:
        try:
            for i in range(25):
                store.record_run(
                    check_type=ScheduleCheckType.COVERAGE,
                    started_at=now + timedelta(seconds=i + n * 100),
                    finished_at=now + timedelta(seconds=i + n * 100),
                    result="PASS" if i % 2 == 0 else "FAIL",
                    exit_code=0 if i % 2 == 0 else 1,
                    duration_seconds=0.01,
                    next_run=now + timedelta(minutes=10),
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        try:
            for _ in range(50):
                store.get_job_state(ScheduleCheckType.COVERAGE)
                store.history(limit=10)
                time.sleep(0.001)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=(0,)),
        threading.Thread(target=writer, args=(1,)),
        threading.Thread(target=reader),
        threading.Thread(target=reader),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert store.get_job_state(ScheduleCheckType.COVERAGE) is not None
    assert integrity_check_ok(store._conn) is True
    store.close()


def test_schedule_rejects_newer_schema(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    store = ScheduleStore(db)
    store.close()
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE meta SET value = ? WHERE key = 'schema_version'",
        (str(SCHEDULE_STORE_SCHEMA_VERSION + 5),),
    )
    conn.commit()
    conn.close()
    with pytest.raises(ScheduleStoreError, match="newer than supported"):
        ScheduleStore(db)


def test_schedule_uncommitted_run_does_not_persist(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    store = ScheduleStore(db)
    try:
        now = datetime.now(UTC)
        store.record_run(
            check_type=ScheduleCheckType.INTEGRITY,
            started_at=now,
            finished_at=now,
            result="PASS",
            exit_code=0,
            duration_seconds=0.1,
            next_run=now + timedelta(hours=1),
        )
        store._conn.execute("BEGIN IMMEDIATE")
        store._conn.execute(
            """
            INSERT INTO runs (
              check_type, started_at, finished_at, result, exit_code,
              duration_seconds, detail
            ) VALUES ('coverage', ?, ?, 'PASS', 0, 0.1, 'ghost')
            """,
            (now.isoformat(), now.isoformat()),
        )
        store._conn.close()
    except Exception:
        store.close()
        raise

    store2 = ScheduleStore(db)
    try:
        hist = store2.history(limit=20)
        assert len(hist) == 1
        assert hist[0].check_type == ScheduleCheckType.INTEGRITY
        assert store2.get_job_state(ScheduleCheckType.COVERAGE) is None
        assert integrity_check_ok(store2._conn) is True
    finally:
        store2.close()
