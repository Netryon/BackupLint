"""Service/mount assurance projection for the dashboard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from backuplint.events import new_event_id
from backuplint.fleet.controller_store import ControllerStore
from backuplint.fleet.dashboard.assurance import project_assurance
from backuplint.fleet.dashboard.config import DashboardConfig
from backuplint.fleet.dashboard.query import (
    OFFLINE,
    STALE,
    DashboardQueryService,
)
from backuplint.fleet.dashboard.ui import page_agent, page_service
from backuplint.fleet.protocol import PROTOCOL_VERSION, ResultEnvelope


def _store(tmp_path: Path) -> ControllerStore:
    return ControllerStore(tmp_path / "c.sqlite3")


def _ingest(
    store: ControllerStore,
    agent_id: str,
    result: dict[str, object],
    *,
    scan: str,
    status_hint: str | None = None,
) -> None:
    payload = dict(result)
    if status_hint and "result" not in payload:
        payload["result"] = status_hint
    env = ResultEnvelope(
        protocol_version=PROTOCOL_VERSION,
        agent_id=agent_id,
        submission_id=new_event_id(),
        scan_time=scan,
        backuplint_version="1.0.0",
        platform="test",
        result=payload,
    )
    store.ingest_result(env)


def _finding(
    service: str,
    path: str,
    status: str,
    *,
    target: str | None = None,
    storage_class: str = "persistent",
    detail: str = "",
    covered_by: str | None = None,
    snapshot_time: str | None = None,
    project: str = "stack",
) -> dict[str, object]:
    return {
        "service": service,
        "status": status,
        "path": path,
        "target": target or path,
        "type": "bind",
        "storage_class": storage_class,
        "detail": detail,
        "covered_by": covered_by,
        "critical": status == "not protected",
        "snapshot_time": snapshot_time,
        "project": project,
    }


def test_project_assurance_sibling_and_mounts() -> None:
    payload = {
        "result": "FAIL",
        "summary": {"protected": 2, "warning": 0, "critical": 1, "skipped": 1},
        "findings": [
            _finding(
                "postgres",
                "/srv/db",
                "protected",
                detail="backed up 2m ago",
                covered_by="/srv/db",
                snapshot_time="2026-09-22T12:00:00Z",
            ),
            _finding(
                "postgres",
                "/var/lib/postgresql/data",
                "not protected",
                detail="persistent mount is not covered",
            ),
            _finding(
                "files",
                "/srv/app/files",
                "protected",
                detail="backed up 2m ago",
                covered_by="/srv/app/files",
                snapshot_time="2026-09-22T12:00:00Z",
            ),
            _finding(
                "files",
                "/tmp/cache",
                "skipped",
                storage_class="cache",
                detail="cache skipped",
            ),
            _finding(
                "redis",
                "redisdata",
                "protected",
                target="/data",
                detail="backed up 2m ago",
                covered_by="redisdata",
                snapshot_time="2026-09-22T11:00:00Z",
            ),
        ],
        "integrity": {"status": "passed", "mode": "standard", "message": "ok"},
        "restore_verification": {"status": "not_requested"},
    }
    payload["findings"][4]["type"] = "volume"
    view = project_assurance(payload, agent_id="agent-a", occurred_at="t0")
    services = {
        s["service"]: s
        for p in view["projects"]
        for s in p["services"]
    }
    assert set(services) == {"postgres", "files", "redis"}
    assert services["postgres"]["overall"] == "FAIL"
    assert services["files"]["overall"] == "PASS"
    assert services["redis"]["overall"] == "PASS"
    assert services["postgres"]["restore"] == "NOT_RUN"
    pg_mounts = {m["host_path"]: m["coverage"] for m in services["postgres"]["mounts"]}
    assert pg_mounts["/srv/db"] == "PASS"
    assert pg_mounts["/var/lib/postgresql/data"] == "FAIL"
    assert view["failing_count"] == 1
    assert view["healthy_count"] == 2
    alerts = [a for a in view["alerts"] if a["service"] == "postgres"]
    assert any(a["mount"] == "/var/lib/postgresql/data" for a in alerts)
    assert all(a["service"] != "files" for a in view["alerts"] if a.get("check") == "coverage")


def test_repository_error_is_not_coverage_fail() -> None:
    view = project_assurance(
        {
            "result": "ERROR",
            "findings": [],
            "operational_error": {
                "kind": "timeout",
                "message": "restic list snapshots timed out",
            },
        },
        agent_id="ag-repo",
    )
    assert view["repository"]["state"] == "ERROR"
    assert view["integrity"]["state"] == "ERROR"
    assert view["alerts"][0]["check"] == "repository"
    assert view["alerts"][0]["state"] == "ERROR"
    assert all(a["check"] != "coverage" for a in view["alerts"])


def test_old_payload_without_findings_degrades() -> None:
    view = project_assurance(
        {"summary": {"result": "PASS"}},
        agent_id="ag-old",
        audit_status="PASS",
    )
    assert view["projects"] == []
    assert view["empty_reason"]
    assert view["restore_verification"]["state"] == "NOT_RUN"
    assert view["integrity"]["state"] == "NOT_RUN"
    assert view["persistent_service_count"] == 0


def test_query_two_services_history_and_offline(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        agent = "agent-a01"
        store.register_agent(agent_id=agent, label="app", hostname="appn.example")
        t0 = "2026-09-22T10:00:00+00:00"
        t1 = "2026-09-22T11:00:00+00:00"
        healthy = {
            "result": "PASS",
            "findings": [
                _finding(
                    "postgres",
                    "/srv/db",
                    "protected",
                    snapshot_time=t0,
                    detail="backed up",
                    covered_by="/srv/db",
                ),
                _finding(
                    "files",
                    "/srv/files",
                    "protected",
                    snapshot_time=t0,
                    detail="backed up",
                    covered_by="/srv/files",
                ),
            ],
            "integrity": {"status": "passed", "mode": "standard"},
            "restore_verification": {"status": "passed", "mode": "isolated"},
        }
        failed = {
            "result": "FAIL",
            "findings": [
                _finding(
                    "postgres",
                    "/srv/db",
                    "protected",
                    snapshot_time=t1,
                    detail="backed up",
                    covered_by="/srv/db",
                ),
                _finding(
                    "files",
                    "/srv/files",
                    "not protected",
                    detail="not covered",
                ),
            ],
            "integrity": {"status": "passed", "mode": "standard"},
            "restore_verification": {"status": "passed", "mode": "isolated"},
        }
        _ingest(store, agent, healthy, scan=t0, status_hint="PASS")
        _ingest(store, agent, failed, scan=t1, status_hint="FAIL")
        for i in range(30):
            _ingest(
                store,
                agent,
                failed if i % 2 else healthy,
                scan=f"2026-09-22T12:{i:02d}:00+00:00",
                status_hint="FAIL" if i % 2 else "PASS",
            )
        q = DashboardQueryService(
            store,
            DashboardConfig(
                enabled=True, online_after_seconds=60, stale_after_seconds=120
            ),
        )
        now = datetime(2026, 9, 22, 18, 0, tzinfo=UTC)
        detail = q.agent_detail(agent, now=now)
        assert detail is not None
        assert detail["presence"] == OFFLINE
        assurance = detail["assurance"]
        services = {
            s["service"]: s
            for p in assurance["projects"]
            for s in p["services"]
        }
        assert services["postgres"]["overall"] == "PASS"
        assert services["files"]["overall"] == "FAIL"
        svc = q.agent_service(agent, "files", now=now)
        assert svc is not None
        assert svc["service"]["overall"] == "FAIL"
        hist = q.list_events(agent_id=agent, limit=10, offset=0)
        assert hist["limit"] <= 50
        assert len(detail["assurance_history"]) <= 25
        agents = q.list_agents(now=now)
        item = agents["items"][0]
        assert item["assurance"]["failing_count"] == 1
        assert item["assurance"]["healthy_count"] == 1
        stale = q.agent_detail(
            agent, now=datetime(2026, 9, 22, 11, 1, tzinfo=UTC)
        )
        # still no heartbeat
        assert stale["presence"] in {OFFLINE, STALE}
    finally:
        store.close()


def test_stale_heartbeat_keeps_assurance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        agent = "ag-stale-01"
        store.register_agent(agent_id=agent, label="s", hostname="s.example")
        store.heartbeat(
            agent,
            protocol_version=PROTOCOL_VERSION,
            software_version="1.0.0",
        )
        hb = store.last_heartbeat(agent)
        assert hb is not None
        hb_dt = datetime.fromisoformat(hb.replace("Z", "+00:00"))
        now = hb_dt + timedelta(seconds=90)
        _ingest(
            store,
            agent,
            {
                "result": "PASS",
                "findings": [
                    _finding(
                        "web",
                        "/data",
                        "protected",
                        snapshot_time=now.isoformat(),
                        detail="backed up",
                    )
                ],
            },
            scan=now.isoformat(),
            status_hint="PASS",
        )
        q = DashboardQueryService(
            store,
            DashboardConfig(
                enabled=True, online_after_seconds=60, stale_after_seconds=120
            ),
        )
        detail = q.agent_detail(agent, now=now)
        assert detail["presence"] == STALE
        assert detail["current_audit"]["status"] == "PASS"
        assert detail["assurance"]["healthy_count"] == 1
    finally:
        store.close()


def test_ui_escapes_service_and_path() -> None:
    payload = project_assurance(
        {
            "result": "FAIL",
            "findings": [
                {
                    "service": "<script>alert(1)</script>",
                    "status": "not protected",
                    "path": "/var/<img src=x>",
                    "target": "/data",
                    "type": "bind",
                    "storage_class": "persistent",
                    "detail": "not covered <b>x</b>",
                    "project": "proj<script>",
                }
            ],
        },
        agent_id="ag-xss-01",
    )
    html = page_agent(
        {
            "identity": {"agent_id": "ag-xss-01", "label": "x", "hostname": "h"},
            "presence": "online",
            "current_audit": {"status": "FAIL", "occurred_at": "t"},
            "assurance": payload,
            "assurance_history": [],
            "recent_history": [],
            "capability_summary": {"families": {}},
            "latest_by_check_type": {},
        },
        csrf="token",
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x>" not in html
    svc_html = page_service(
        {
            "identity": {"agent_id": "ag-xss-01"},
            "service": payload["projects"][0]["services"][0],
            "history": [],
            "integrity": {},
            "restore_verification": {},
        },
        csrf="token",
    )
    assert "<script>" not in svc_html
    assert "&lt;img" in svc_html or "&lt;script&gt;" in html
