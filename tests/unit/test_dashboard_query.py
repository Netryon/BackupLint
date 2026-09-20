"""Dashboard query semantics and pagination."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from backuplint.events import EVENT_SCHEMA_VERSION, EventType, new_event_id, new_run_id
from backuplint.fleet.controller_store import ControllerStore
from backuplint.fleet.dashboard.config import DashboardConfig
from backuplint.fleet.dashboard.query import (
    OFFLINE,
    ONLINE,
    STALE,
    DashboardQueryService,
    classify_presence,
)
from backuplint.fleet.protocol import PROTOCOL_VERSION, ResultEnvelope
from backuplint.fleet.queue_policy import DATA_GAP_RESULT_MARKER, QUEUE_OVERFLOW_STATUS


def test_presence_separate_from_audit_clock() -> None:
    now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    assert (
        classify_presence(
            (now - timedelta(seconds=30)).isoformat(),
            now=now,
            online_after_seconds=120,
            stale_after_seconds=600,
        )
        == ONLINE
    )
    assert (
        classify_presence(
            (now - timedelta(seconds=300)).isoformat(),
            now=now,
            online_after_seconds=120,
            stale_after_seconds=600,
        )
        == STALE
    )
    assert (
        classify_presence(
            (now - timedelta(hours=2)).isoformat(),
            now=now,
            online_after_seconds=120,
            stale_after_seconds=600,
        )
        == OFFLINE
    )
    assert (
        classify_presence(
            None, now=now, online_after_seconds=120, stale_after_seconds=600
        )
        == OFFLINE
    )


def _ingest_audit(store: ControllerStore, agent_id: str, *, status: str, scan: str) -> None:
    env = ResultEnvelope(
        protocol_version=PROTOCOL_VERSION,
        agent_id=agent_id,
        submission_id=new_event_id(),
        scan_time=scan,
        backuplint_version="0.5.0.dev0",
        platform="test",
        result={"summary": {"result": status}},
    )
    store.ingest_result(env)


def test_overview_keeps_offline_pass_distinct(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "c.sqlite3")
    try:
        agent = "ag-offline-pass"
        store.register_agent(agent_id=agent, label="lab", hostname="lab.example")
        _ingest_audit(
            store,
            agent,
            status="PASS",
            scan="2026-01-01T00:00:00+00:00",
        )
        # No heartbeat → offline presence, but audit still PASS.
        q = DashboardQueryService(
            store,
            DashboardConfig(enabled=True, online_after_seconds=60, stale_after_seconds=120),
        )
        detail = q.agent_detail(agent)
        assert detail is not None
        assert detail["presence"] == OFFLINE
        assert detail["current_audit"]["status"] == "PASS"
        overview = q.fleet_overview()
        assert overview["totals"]["offline"] >= 1
        assert overview["totals"]["audit"]["PASS"] >= 1
    finally:
        store.close()


def test_data_gap_visible_and_events_paginated(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "c.sqlite3")
    try:
        agent = "ag-gap"
        store.register_agent(agent_id=agent, label="gap", hostname="gap")
        now = datetime.now(UTC).isoformat()
        store.heartbeat(agent, protocol_version=PROTOCOL_VERSION, software_version="0.5.0.dev0")
        with store._lock:  # noqa: SLF001
            store._conn.execute(  # noqa: SLF001
                """
                INSERT INTO events (
                  event_id, schema_version, event_type, agent_id, run_id,
                  submission_id, occurred_at, received_at, status, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_event_id(),
                    EVENT_SCHEMA_VERSION,
                    EventType.AUDIT_COMPLETED.value,
                    agent,
                    new_run_id(),
                    new_event_id(),
                    now,
                    now,
                    QUEUE_OVERFLOW_STATUS,
                    (
                        '{"result":{"queue_marker":"'
                        + DATA_GAP_RESULT_MARKER
                        + '","summary":{"result":"'
                        + DATA_GAP_RESULT_MARKER
                        + '"}}}'
                    ),
                ),
            )
        q = DashboardQueryService(store, DashboardConfig(enabled=True, max_page_size=10))
        agents = q.list_agents(has_data_gap=True)
        assert agents["matched"] >= 1
        assert agents["items"][0]["has_data_gap"] is True
        page1 = q.list_events(agent_id=agent, limit=1, offset=0)
        assert page1["returned"] == 1
        assert page1["limit"] == 1
        # Oversized limit is clamped
        big = q.list_events(agent_id=agent, limit=9999)
        assert big["limit"] == 10
    finally:
        store.close()


def test_alerts_trend_and_time_window(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "c.sqlite3")
    try:
        now = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
        agent = "ag-alerts"
        store.register_agent(agent_id=agent, label="a", hostname="a")
        _ingest_audit(store, agent, status="PASS", scan="2026-09-20T11:00:00+00:00")
        _ingest_audit(store, agent, status="FAIL", scan="2026-09-20T11:30:00+00:00")
        _ingest_audit(store, agent, status="WARN", scan="2026-09-20T11:45:00+00:00")
        q = DashboardQueryService(store, DashboardConfig(enabled=True))
        window = q.resolve_time_window(time_range="24h", now=now)
        alerts = q.list_recent_alerts(
            time_from=window["time_from"],
            time_to=window["time_to"],
        )
        kinds = {str(i["alert_kind"]) for i in alerts["items"]}
        assert "FAIL" in kinds
        assert "WARN" not in kinds
        trend = q.audit_status_trend(
            time_from=window["time_from"],
            time_to=window["time_to"],
        )
        assert trend["totals"]["PASS"] >= 1
        assert trend["totals"]["FAIL"] >= 1
        assert trend["totals"]["WARN"] >= 1
        insights = q.dashboard_insights(time_range="1h", now=now)
        assert insights["window"]["time_range"] == "1h"
        assert "alerts" in insights and "trend" in insights
    finally:
        store.close()


def test_capability_not_rewritten_as_fail(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "c.sqlite3")
    try:
        agent = "ag-caps"
        store.register_agent(agent_id=agent, label="c", hostname="c")
        caps = {
            "schema_version": 1,
            "deployment_form": "native",
            "capabilities": {
                "restore_verification": {
                    "installed": False,
                    "supported": True,
                    "available": False,
                    "reason": "NOT_CONFIGURED",
                }
            },
        }
        store.heartbeat(
            agent,
            protocol_version=PROTOCOL_VERSION,
            software_version="0.5.0.dev0",
            capabilities=caps,
        )
        q = DashboardQueryService(store, DashboardConfig(enabled=True))
        detail = q.agent_detail(agent)
        assert detail is not None
        family = detail["capability_summary"]["families"]["restore_verification"]
        assert family["available"] is False
        assert family["installed"] is False
        assert detail["current_audit"] is None
    finally:
        store.close()
