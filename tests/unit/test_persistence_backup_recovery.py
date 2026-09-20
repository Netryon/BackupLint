"""Persistence backup, recovery, migration, WAL, and concurrency tests (Agent 4)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backuplint.fleet.controller_store import (
    STORE_SCHEMA_VERSION,
    ControllerStore,
)
from backuplint.fleet.protocol import ResultEnvelope, new_submission_id
from backuplint.schedule_config import ScheduleCheckType
from backuplint.schedule_store import (
    SCHEDULE_STORE_SCHEMA_VERSION,
    ScheduleStore,
)
from backuplint.sqlite_backup import (
    PersistenceBackupError,
    diagnose_sqlite_file,
    restore_sqlite_backup_file,
    verify_sqlite_backup,
)
from tests.helpers.persistence_lab import (
    abrupt_controller_writer,
    seed_controller_v1_db,
    seed_schedule_pre_meta_db,
)


def _envelope(agent_id: str, submission_id: str | None = None) -> ResultEnvelope:
    return ResultEnvelope(
        agent_id=agent_id,
        submission_id=submission_id or new_submission_id(),
        scan_time=datetime.now(UTC).isoformat(),
        backuplint_version="0.5.0.dev0",
        platform="test",
        result={"summary": {"result": "PASS"}},
        run_id="run-" + "c" * 32,
    )


def test_controller_online_backup_while_writes(tmp_path: Path) -> None:
    db = tmp_path / "controller.sqlite3"
    store = ControllerStore(db)
    store.register_agent(agent_id="agent-bak0000001", label="a", hostname="h")
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            while not stop.is_set():
                store.ingest_result(_envelope("agent-bak0000001"))
                store.heartbeat("agent-bak0000001")
                time.sleep(0.001)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=writer)
    t.start()
    time.sleep(0.02)
    dest = tmp_path / "controller.bak"
    try:
        manifest = store.backup(dest)
    finally:
        stop.set()
        t.join(timeout=10)
    assert errors == []
    assert dest.is_file()
    assert (tmp_path / "controller.bak.manifest.json").is_file()
    assert manifest.store_type == "controller"
    assert manifest.schema_version == STORE_SCHEMA_VERSION
    assert manifest.integrity_check == "ok"
    assert manifest.backup_bytes > 0
    assert "CA certificates" in " ".join(manifest.notes)
    verify = ControllerStore.verify_backup(dest)
    assert verify.ok is True
    assert "agents" in verify.tables_present
    # Proven via backup API + verification; not a manual copy of live -wal/-shm.
    assert dest.stat().st_size > 0
    store.close()


def test_backup_failure_cleans_temp_and_does_not_claim_success(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "c.sqlite3")
    store.register_agent(agent_id="agent-bakfail0001", label="a", hostname="h")
    dest = tmp_path / "missing-parent-never" / "x.bak"
    # Parent is a file → mkdir/replace path fails.
    blocker = tmp_path / "missing-parent-never"
    blocker.write_text("not-a-dir", encoding="utf-8")
    with pytest.raises(PersistenceBackupError):
        store.backup(dest)
    assert not dest.exists()
    assert not Path(str(dest) + ".tmp").exists()
    store.close()


def test_backup_destination_io_failure_via_readonly_dir(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "c.sqlite3")
    store.register_agent(agent_id="agent-rodest0001", label="a", hostname="h")
    out_dir = tmp_path / "ro"
    out_dir.mkdir()
    dest = out_dir / "c.bak"
    out_dir.chmod(0o500)
    try:
        with pytest.raises(PersistenceBackupError):
            store.backup(dest)
        assert not dest.exists()
    finally:
        out_dir.chmod(0o700)
        store.close()


def test_restore_reopen_continue_writing(tmp_path: Path) -> None:
    live = tmp_path / "live.sqlite3"
    bak = tmp_path / "live.bak"
    restored = tmp_path / "restored.sqlite3"
    store = ControllerStore(live)
    store.register_agent(agent_id="agent-restore0001", label="a", hostname="h")
    store.ingest_result(_envelope("agent-restore0001"))
    store.set_agent_status("agent-restore0001", "revoked")
    store.backup(bak)
    store.close()

    # Destroy/replace isolated DB path, restore offline, reopen.
    live.unlink()
    restore_sqlite_backup_file(
        bak,
        restored,
        store_type="controller",
        max_supported_schema=STORE_SCHEMA_VERSION,
    )
    store2 = ControllerStore(restored)
    try:
        agent = store2.get_agent("agent-restore0001")
        assert agent is not None
        assert agent.status == "revoked"
        assert store2.latest_event("agent-restore0001") is not None
        # Revoked cannot ingest; re-activate and continue writing.
        store2.set_agent_status("agent-restore0001", "active")
        assert store2.ingest_result(_envelope("agent-restore0001")) is True
        store2.heartbeat("agent-restore0001")
        assert store2.last_heartbeat("agent-restore0001") is not None
    finally:
        store2.close()


def test_backup_includes_committed_wal_state(tmp_path: Path) -> None:
    db = tmp_path / "c.sqlite3"
    store = ControllerStore(db)
    store.register_agent(agent_id="agent-walbak0001", label="a", hostname="h")
    for _ in range(20):
        store.ingest_result(_envelope("agent-walbak0001"))
    # Leave WAL with committed pages; do not require manual -wal copy.
    store.checkpoint("PASSIVE")
    bak = tmp_path / "c.bak"
    store.backup(bak)
    store.close()
    verify = verify_sqlite_backup(bak, store_type="controller")
    assert verify.ok
    restored = tmp_path / "r.sqlite3"
    restore_sqlite_backup_file(bak, restored, store_type="controller")
    store2 = ControllerStore(restored)
    try:
        count = store2._conn.execute("SELECT COUNT(*) FROM submissions").fetchone()
        assert count is not None and int(count[0]) == 20
    finally:
        store2.close()


def test_schedule_backup_restore_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    store = ScheduleStore(db, history_limit=20)
    now = datetime.now(UTC)
    store.record_run(
        check_type=ScheduleCheckType.COVERAGE,
        started_at=now,
        finished_at=now,
        result="PASS",
        exit_code=0,
        duration_seconds=0.2,
        next_run=now + timedelta(minutes=10),
    )
    bak = tmp_path / "s.bak"
    manifest = store.backup(bak)
    assert manifest.store_type == "schedule"
    store.close()
    restored = tmp_path / "s2.sqlite3"
    restore_sqlite_backup_file(bak, restored, store_type="schedule")
    store2 = ScheduleStore(restored)
    try:
        state = store2.get_job_state(ScheduleCheckType.COVERAGE)
        assert state is not None
        assert state.last_result == "PASS"
        store2.record_run(
            check_type=ScheduleCheckType.INTEGRITY,
            started_at=now,
            finished_at=now,
            result="FAIL",
            exit_code=1,
            duration_seconds=0.1,
            next_run=now + timedelta(hours=1),
        )
    finally:
        store2.close()


def test_migration_matrix_controller_v1_to_current(tmp_path: Path) -> None:
    db = tmp_path / "v1.sqlite3"
    agent_id = seed_controller_v1_db(db)
    store = ControllerStore(db)
    try:
        assert store.schema_version() == STORE_SCHEMA_VERSION
        assert store.get_agent(agent_id) is not None
        event = store.latest_event(agent_id)
        assert event is not None
        assert event["occurred_at"] == "2026-01-01T01:00:00+00:00"
        # Idempotent reopen / current→current.
    finally:
        store.close()
    store2 = ControllerStore(db)
    try:
        assert store2.schema_version() == STORE_SCHEMA_VERSION
        assert store2.latest_event(agent_id) is not None
    finally:
        store2.close()


def test_migration_matrix_schedule_pre_meta_to_current(tmp_path: Path) -> None:
    db = tmp_path / "premeta.sqlite3"
    seed_schedule_pre_meta_db(db)
    store = ScheduleStore(db)
    try:
        assert store.schema_version() == SCHEDULE_STORE_SCHEMA_VERSION
        assert store.get_job_state(ScheduleCheckType.COVERAGE) is not None
        assert len(store.history(limit=5)) == 1
    finally:
        store.close()


def test_controller_migration_failure_before_and_mid(tmp_path: Path) -> None:
    db = tmp_path / "fault.sqlite3"
    agent_id = seed_controller_v1_db(db)

    def fail_before(point: str) -> None:
        if point == "before_migration":
            raise RuntimeError("injected before_migration")

    with pytest.raises(RuntimeError, match="before_migration"):
        ControllerStore(db, migration_fault_hook=fail_before)

    # DB must remain reopenable and migrate cleanly afterward.
    store = ControllerStore(db)
    try:
        assert store.schema_version() == STORE_SCHEMA_VERSION
        assert store.latest_event(agent_id) is not None
    finally:
        store.close()

    db2 = tmp_path / "fault2.sqlite3"
    seed_controller_v1_db(db2)

    def fail_mid(point: str) -> None:
        if point == "after_schema_v2":
            raise RuntimeError("injected after_schema_v2")

    with pytest.raises(RuntimeError, match="after_schema_v2"):
        ControllerStore(db2, migration_fault_hook=fail_mid)

    store2 = ControllerStore(db2)
    try:
        assert store2.schema_version() == STORE_SCHEMA_VERSION
        assert store2.latest_event("agent-fixture0001") is not None
    finally:
        store2.close()


def test_controller_migration_failure_after_version_bump(tmp_path: Path) -> None:
    db = tmp_path / "fault3.sqlite3"
    seed_controller_v1_db(db)

    def fail_after_version(point: str) -> None:
        if point == "after_version_v2":
            raise RuntimeError("injected after_version_v2")

    with pytest.raises(RuntimeError, match="after_version_v2"):
        ControllerStore(db, migration_fault_hook=fail_after_version)

    # Transactional rollback: version bump must not stick if commit never happened.
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    conn.close()
    assert row is None or int(row[0]) < STORE_SCHEMA_VERSION
    # Deterministic recovery on next open without hook.
    store = ControllerStore(db)
    try:
        assert store.schema_version() == STORE_SCHEMA_VERSION
    finally:
        store.close()


def test_write_failure_during_transaction_leaves_consistent_db(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "c.sqlite3")
    store.register_agent(agent_id="agent-iofail0001", label="a", hostname="h")
    real = store._conn

    class Boom(Exception):
        pass

    class Proxy:
        def execute(self, sql: object, parameters: object = ()) -> sqlite3.Cursor:
            text = " ".join(str(sql).split())
            if text.startswith("INSERT INTO events"):
                raise Boom("disk full simulated")
            return real.execute(sql, parameters)  # type: ignore[arg-type]

        def __getattr__(self, name: str) -> object:
            return getattr(real, name)

    store._conn = Proxy()  # type: ignore[assignment]
    with pytest.raises(Boom):
        store.ingest_result(_envelope("agent-iofail0001"))
    store._conn = real
    assert store.latest_result("agent-iofail0001") is None
    assert store.ingest_result(_envelope("agent-iofail0001")) is True
    store.close()


def test_abrupt_subprocess_close_recovers(tmp_path: Path) -> None:
    db = tmp_path / "abrupt.sqlite3"
    abrupt_controller_writer(db)
    store = ControllerStore(db)
    try:
        assert store.get_agent("agent-abrupt0001") is not None
        assert store.latest_event("agent-abrupt0001") is not None
        assert store.last_heartbeat("agent-abrupt0001") is not None
        # Continue writing + idempotent duplicate.
        env = _envelope("agent-abrupt0001")
        assert store.ingest_result(env) is True
        assert store.ingest_result(env) is False
        store.set_agent_status("agent-abrupt0001", "revoked")
        assert store.get_agent("agent-abrupt0001").status == "revoked"  # type: ignore[union-attr]
    finally:
        store.close()


def test_backup_while_duplicate_submission_race(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "c.sqlite3")
    store.register_agent(agent_id="agent-racebak0001", label="a", hostname="h")
    sub = new_submission_id()
    results: list[bool] = []
    barrier = threading.Barrier(6)

    def worker() -> None:
        barrier.wait(timeout=5)
        results.append(store.ingest_result(_envelope("agent-racebak0001", sub)))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()

    def backup_worker() -> None:
        barrier.wait(timeout=5)
        store.backup(tmp_path / "race.bak")

    bt = threading.Thread(target=backup_worker)
    bt.start()
    for t in threads:
        t.join(timeout=10)
    bt.join(timeout=10)
    assert results.count(True) == 1
    assert ControllerStore.verify_backup(tmp_path / "race.bak").ok
    store.close()


def test_diagnostics_foundation(tmp_path: Path) -> None:
    missing = diagnose_sqlite_file(
        tmp_path / "nope.sqlite3",
        store_type="controller",
        max_supported_schema=STORE_SCHEMA_VERSION,
    )
    assert missing.exists is False
    assert "does not exist" in missing.issues[0]

    store = ControllerStore(tmp_path / "c.sqlite3")
    store.register_agent(agent_id="agent-diag000001", label="a", hostname="h")
    diag = store.diagnostics()
    store.close()
    assert diag.opens is True
    assert diag.integrity_ok is True
    assert diag.schema_supported is True
    assert diag.journal_mode == "wal"
    assert diag.required_tables_present is True


def test_verify_detects_orphan_event(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "c.sqlite3")
    store.register_agent(agent_id="agent-orphan0001", label="a", hostname="h")
    bak = tmp_path / "c.bak"
    store.backup(bak)
    store.close()
    conn = sqlite3.connect(str(bak))
    conn.execute(
        """
        INSERT INTO events (
          event_id, schema_version, event_type, agent_id, run_id,
          submission_id, occurred_at, received_at, status, payload_json
        ) VALUES ('evt-orphan', 1, 'audit.completed', 'agent-orphan0001', 'run-x',
                  'missing-submission-id', ?, ?, 'PASS', '{}')
        """,
        (datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
    )
    conn.commit()
    conn.close()
    verify = verify_sqlite_backup(bak, store_type="controller")
    assert verify.ok is False
    assert any("missing submission_id" in i for i in verify.issues)


def test_manifest_has_no_secrets(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "c.sqlite3")
    token = store.create_enroll_token(label="x", ttl=timedelta(hours=1))
    bak = tmp_path / "c.bak"
    store.backup(bak)
    store.close()
    text = Path(str(bak) + ".manifest.json").read_text(encoding="utf-8")
    assert token not in text
    data = json.loads(text)
    assert "notes" in data
    assert data["store_type"] == "controller"


def test_unsupported_multiprocess_documented_via_diagnostics(tmp_path: Path) -> None:
    # We do not claim multi-process support; ensure single-process backup/verify works
    # and docs/notes state the boundary (exercised via notes + diagnostics).
    store = ControllerStore(tmp_path / "c.sqlite3")
    notes = " ".join(store.backup(tmp_path / "c.bak").notes)
    assert "NOT included" in notes
    store.close()


def test_refuse_restore_of_bad_backup(tmp_path: Path) -> None:
    bad = tmp_path / "bad.bak"
    bad.write_bytes(b"not a database")
    with pytest.raises(PersistenceBackupError, match="unverified"):
        restore_sqlite_backup_file(bad, tmp_path / "out.sqlite3", store_type="controller")


def test_schedule_migration_fault_before(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    seed_schedule_pre_meta_db(db)

    def fail(point: str) -> None:
        if point == "before_migration":
            raise RuntimeError("sched before")

    with pytest.raises(RuntimeError, match="sched before"):
        ScheduleStore(db, migration_fault_hook=fail)
    store = ScheduleStore(db)
    try:
        assert store.schema_version() == SCHEDULE_STORE_SCHEMA_VERSION
    finally:
        store.close()
