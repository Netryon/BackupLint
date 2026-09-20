"""Pre-v1 hardening unit tests for deferred findings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backuplint.fleet.controller_store import ControllerStore
from backuplint.fleet.protocol import ResultEnvelope, new_submission_id
from backuplint.fleet.queue_policy import SaturatedAgentQueue, corrupt_line_submission_id
from backuplint.install.layout import LayoutError, require_service_account
from backuplint.restore_dest import RestoreDestinationError, create_owned_restore_root


def test_corrupt_queue_line_stable_across_reloads(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    path.write_text("{not-json\n", encoding="utf-8")
    q1 = SaturatedAgentQueue(path, max_items=10, reserved_critical=2)
    items1 = q1.peek_all()
    assert len(items1) == 1
    sid = items1[0]["submission_id"]
    assert sid == corrupt_line_submission_id("{not-json")
    q2 = SaturatedAgentQueue(path, max_items=10, reserved_critical=2)
    items2 = q2.peek_all()
    assert items2[0]["submission_id"] == sid
    q2.remove(str(sid))
    assert q2.peek_all() == []


def test_non_object_queue_line_becomes_data_gap(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    path.write_text('"just-a-string"\n', encoding="utf-8")
    q = SaturatedAgentQueue(path, max_items=10, reserved_critical=2)
    items = q.peek_all()
    assert items[0]["result"]["data_gap"] is True
    assert items[0]["result"]["gap"]["reason"] == "NON_OBJECT_QUEUE_RECORD"


def test_hostile_tmpdir_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hostile = tmp_path / "etc"
    hostile.mkdir()
    # Pretend TMPDIR is under /etc by using assert path via parent=hostile
    # Direct parent under forbidden prefix:
    with pytest.raises(RestoreDestinationError):
        create_owned_restore_root(parent=Path("/etc"))
    # Env TMPDIR=/etc should fall back rather than create under /etc
    monkeypatch.setenv("TMPDIR", "/etc")
    # tempfile.gettempdir may still cache; force parent via None after cache clear
    import tempfile

    tempfile.tempdir = None
    owned = create_owned_restore_root()
    assert "/etc/" not in str(owned.path)
    assert owned.path.exists()


def test_service_account_missing_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import pwd

    def boom(_name: str) -> object:
        raise KeyError("missing")

    monkeypatch.setattr(pwd, "getpwnam", boom)
    with pytest.raises(LayoutError, match="service user"):
        require_service_account(user="no-such-backuplint-user")


def test_prune_operational_history_keeps_latest(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "c.sqlite3")
    store.register_agent(agent_id="agent-ret01", label="a", hostname="h")
    old = (datetime.now(UTC) - timedelta(days=120)).isoformat()
    new = datetime.now(UTC).isoformat()
    # Insert via ingest for new, and direct SQL for aged row.
    env = ResultEnvelope(
        agent_id="agent-ret01",
        submission_id=new_submission_id(),
        scan_time=new,
        backuplint_version="0.8.0",
        platform="test",
        result={"summary": {"result": "PASS"}},
    )
    store.ingest_result(env)
    with store._lock:  # noqa: SLF001
        store._conn.execute(  # noqa: SLF001
            """
            INSERT INTO events (
              event_id, schema_version, event_type, agent_id, run_id,
              submission_id, occurred_at, received_at, status, payload_json
            ) VALUES (?, 1, 'AUDIT_COMPLETED', 'agent-ret01', 'run-old',
                      'sub-old', ?, ?, 'PASS', '{}')
            """,
            ("evt-old", old, old),
        )
        store._conn.execute(  # noqa: SLF001
            """
            INSERT INTO submissions (
              submission_id, agent_id, scan_time, received_time,
              backuplint_version, platform, result_json, protocol_version
            ) VALUES ('sub-old', 'agent-ret01', ?, ?, '0.8.0', 't', '{}', 1)
            """,
            (old, old),
        )
    stats = store.prune_operational_history(events_keep_days=90, submissions_keep_days=90)
    assert stats["deleted_events"] >= 1
    # Latest AUDIT_COMPLETED from ingest should remain
    with store._lock:  # noqa: SLF001
        remaining = store._conn.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM events WHERE agent_id='agent-ret01'"
        ).fetchone()[0]
    assert remaining >= 1
