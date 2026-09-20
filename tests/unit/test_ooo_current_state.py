"""Deterministic OOO / current-state semantics (occurred_at vs received_at)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backuplint.events import event_is_stale
from backuplint.fleet.controller_store import ControllerStore
from backuplint.fleet.protocol import ResultEnvelope, new_submission_id


def _envelope(
    agent_id: str,
    *,
    scan_time: str,
    seq: str | int | None = None,
    result: str = "PASS",
    submission_id: str | None = None,
) -> ResultEnvelope:
    summary: dict[str, object] = {"result": result}
    if seq is not None:
        summary["seq"] = seq
    return ResultEnvelope(
        agent_id=agent_id,
        submission_id=submission_id or new_submission_id(),
        scan_time=scan_time,
        backuplint_version="0.5.0.dev0",
        platform="ooo-test",
        result={"summary": summary},
    )


def _seq(event: dict[str, object] | None) -> object | None:
    if event is None:
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        return None
    return summary.get("seq")


def _open_agent(tmp_path: Path, agent_id: str = "agent-ooo0000001") -> ControllerStore:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.register_agent(agent_id=agent_id, label="ooo", hostname="host")
    return store


def test_seq_10_then_9_older_does_not_overwrite_current(tmp_path: Path) -> None:
    """Ingest newer occurred_at first, then older — current stays newer."""
    agent_id = "agent-ooo0000001"
    store = _open_agent(tmp_path, agent_id)
    try:
        assert store.ingest_result(
            _envelope(agent_id, scan_time="2026-01-01T10:00:00+00:00", seq=10)
        )
        assert store.ingest_result(
            _envelope(agent_id, scan_time="2026-01-01T09:00:00+00:00", seq=9)
        )
        current = store.current_event_by_occurred_at(agent_id)
        latest = store.latest_event(agent_id)
        assert _seq(current) == 10
        assert current is not None and current["occurred_at"] == "2026-01-01T10:00:00+00:00"
        assert _seq(latest) == 9
        assert latest is not None and latest["occurred_at"] == "2026-01-01T09:00:00+00:00"
        cres = store.current_result_by_occurred_at(agent_id)
        assert cres is not None
        assert cres["result"]["summary"]["seq"] == 10
    finally:
        store.close()


def test_seq_9_then_10_current_and_latest_agree(tmp_path: Path) -> None:
    agent_id = "agent-ooo0000002"
    store = _open_agent(tmp_path, agent_id)
    try:
        assert store.ingest_result(
            _envelope(agent_id, scan_time="2026-01-01T09:00:00+00:00", seq=9)
        )
        assert store.ingest_result(
            _envelope(agent_id, scan_time="2026-01-01T10:00:00+00:00", seq=10)
        )
        current = store.current_event_by_occurred_at(agent_id)
        latest = store.latest_event(agent_id)
        assert _seq(current) == 10
        assert _seq(latest) == 10
    finally:
        store.close()


def test_equal_occurred_at_tie_breaks_by_received(tmp_path: Path) -> None:
    agent_id = "agent-ooo0000003"
    store = _open_agent(tmp_path, agent_id)
    try:
        t = "2026-01-01T12:00:00+00:00"
        assert store.ingest_result(_envelope(agent_id, scan_time=t, seq="first"))
        assert store.ingest_result(_envelope(agent_id, scan_time=t, seq="second"))
        current = store.current_event_by_occurred_at(agent_id)
        latest = store.latest_event(agent_id)
        # Same occurred_at: later receive wins for both helpers.
        assert _seq(current) == "second"
        assert _seq(latest) == "second"
    finally:
        store.close()


def test_delayed_delivery_receive_ooo_transport_vs_health(tmp_path: Path) -> None:
    """B@01:00 then A@00:00: last-received=A, current-by-occurred=B."""
    agent_id = "agent-ooo0000004"
    store = _open_agent(tmp_path, agent_id)
    try:
        assert store.ingest_result(
            _envelope(
                agent_id,
                scan_time="2026-01-01T01:00:00+00:00",
                seq="B",
                result="FAIL",
            )
        )
        assert store.ingest_result(
            _envelope(
                agent_id,
                scan_time="2026-01-01T00:00:00+00:00",
                seq="A",
                result="PASS",
            )
        )
        assert _seq(store.latest_event(agent_id)) == "A"
        assert _seq(store.current_event_by_occurred_at(agent_id)) == "B"
    finally:
        store.close()


def test_duplicate_submission_does_not_change_current(tmp_path: Path) -> None:
    agent_id = "agent-ooo0000005"
    store = _open_agent(tmp_path, agent_id)
    try:
        env = _envelope(agent_id, scan_time="2026-01-01T10:00:00+00:00", seq=10)
        assert store.ingest_result(env) is True
        assert store.ingest_result(env) is False
        older = _envelope(agent_id, scan_time="2026-01-01T09:00:00+00:00", seq=9)
        assert store.ingest_result(older) is True
        assert _seq(store.current_event_by_occurred_at(agent_id)) == 10
    finally:
        store.close()


def test_restart_preserves_current_state(tmp_path: Path) -> None:
    agent_id = "agent-ooo0000006"
    db = tmp_path / "controller.sqlite3"
    store = ControllerStore(db)
    store.register_agent(agent_id=agent_id, label="ooo", hostname="host")
    store.ingest_result(
        _envelope(agent_id, scan_time="2026-01-01T10:00:00+00:00", seq=10)
    )
    store.ingest_result(
        _envelope(agent_id, scan_time="2026-01-01T09:00:00+00:00", seq=9)
    )
    store.close()

    store2 = ControllerStore(db)
    try:
        assert _seq(store2.current_event_by_occurred_at(agent_id)) == 10
        assert _seq(store2.latest_event(agent_id)) == 9
    finally:
        store2.close()


def test_large_backlog_current_not_null(tmp_path: Path) -> None:
    agent_id = "agent-ooo0000007"
    store = _open_agent(tmp_path, agent_id)
    try:
        for i in range(200):
            minute = i % 60
            hour = i // 60
            scan = f"2026-01-01T{hour:02d}:{minute:02d}:00+00:00"
            assert store.ingest_result(
                _envelope(agent_id, scan_time=scan, seq=i, result="PASS")
            )
        # Delayed older event after backlog.
        assert store.ingest_result(
            _envelope(
                agent_id,
                scan_time="2025-12-31T23:00:00+00:00",
                seq="stale",
                result="FAIL",
            )
        )
        current = store.current_event_by_occurred_at(agent_id)
        latest = store.latest_event(agent_id)
        assert current is not None
        assert latest is not None
        assert _seq(current) == 199
        assert _seq(latest) == "stale"
    finally:
        store.close()


def test_mixed_agents_isolated_current(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    a1, a2 = "agent-ooo0000008", "agent-ooo0000009"
    try:
        store.register_agent(agent_id=a1, label="a", hostname="h1")
        store.register_agent(agent_id=a2, label="b", hostname="h2")
        store.ingest_result(
            _envelope(a1, scan_time="2026-01-01T10:00:00+00:00", seq="a-new")
        )
        store.ingest_result(
            _envelope(a2, scan_time="2026-01-01T11:00:00+00:00", seq="b-new")
        )
        store.ingest_result(
            _envelope(a1, scan_time="2026-01-01T09:00:00+00:00", seq="a-old")
        )
        assert _seq(store.current_event_by_occurred_at(a1)) == "a-new"
        assert _seq(store.current_event_by_occurred_at(a2)) == "b-new"
        assert _seq(store.latest_event(a1)) == "a-old"
    finally:
        store.close()


def test_stale_heartbeat_separate_from_last_pass(tmp_path: Path) -> None:
    agent_id = "agent-ooo0000010"
    store = _open_agent(tmp_path, agent_id)
    try:
        old_pass = "2026-01-01T00:00:00+00:00"
        store.ingest_result(
            _envelope(agent_id, scan_time=old_pass, seq="pass", result="PASS")
        )
        store.heartbeat(agent_id)
        hb = store.last_heartbeat(agent_id)
        current = store.current_event_by_occurred_at(agent_id)
        assert hb is not None
        assert current is not None
        assert current["status"] == "PASS"
        assert event_is_stale(
            str(current["occurred_at"]),
            now=datetime(2026, 1, 10, tzinfo=UTC),
            max_age=timedelta(days=1),
        )
        # Fresh heartbeat does not change audit current.
        assert _seq(current) == "pass"
        assert hb != current["occurred_at"]
    finally:
        store.close()


def test_concurrent_ingest_and_current_reads(tmp_path: Path) -> None:
    agent_id = "agent-ooo0000011"
    store = _open_agent(tmp_path, agent_id)
    errors: list[BaseException] = []
    stop = threading.Event()

    def writer(start: int, count: int) -> None:
        try:
            for i in range(start, start + count):
                scan = f"2026-01-01T{(i // 60) % 24:02d}:{i % 60:02d}:00+00:00"
                store.ingest_result(
                    _envelope(agent_id, scan_time=scan, seq=i)
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        try:
            while not stop.is_set():
                cur = store.current_event_by_occurred_at(agent_id)
                lat = store.latest_event(agent_id)
                # Never invent null once writers have started producing rows;
                # both may be None only at the very start.
                if cur is not None:
                    assert cur.get("occurred_at")
                if lat is not None:
                    assert lat.get("received_at")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    readers = [threading.Thread(target=reader) for _ in range(4)]
    writers = [
        threading.Thread(target=writer, args=(0, 80)),
        threading.Thread(target=writer, args=(80, 80)),
    ]
    for t in readers:
        t.start()
    for t in writers:
        t.start()
    for t in writers:
        t.join()
    stop.set()
    for t in readers:
        t.join()
    try:
        assert not errors
        current = store.current_event_by_occurred_at(agent_id)
        latest = store.latest_event(agent_id)
        assert current is not None
        assert latest is not None
        # Current is max occurred_at among ingested; not null due to concurrency.
        assert current["occurred_at"] >= "2026-01-01T00:00:00+00:00"
    finally:
        store.close()
