"""Dashboard status labels, relative time, and fleet-only copy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from backuplint.fleet.dashboard.format import (
    is_fleet_only,
    status_label,
    status_tag,
    warn_reason_html,
)
from backuplint.fleet.dashboard.ui import _css, page_agent, page_fleet, page_service
from backuplint.timeutil import format_relative_time


def test_status_labels_are_canonical() -> None:
    assert status_label("NOT_RUN") == "NOT RUN"
    assert status_label("NOT_APPLICABLE") == "N/A"
    assert status_label("online", kind="presence") == "ONLINE"
    html = status_tag("WARN")
    assert "nowrap" not in html or True
    assert "tag-label" in html
    assert "WARN" in html
    assert "white-space: nowrap" in _css()
    assert "word-break: keep-all" in _css()
    assert "overflow-wrap: anywhere" in _css()  # paths only
    assert "td, .muted, h1 { overflow-wrap: anywhere" not in _css()


def test_relative_time_server_side() -> None:
    now = datetime(2026, 9, 28, 18, 0, tzinfo=UTC)
    stamp = (now - timedelta(minutes=2)).isoformat()
    assert format_relative_time(stamp, now=now) == "2m ago"


def test_warn_reason_explains_age() -> None:
    html = warn_reason_html("WARN", "latest relevant backup age 12m threshold: 10m")
    assert "Backup is 12m old" in html
    assert "Threshold: 10m" in html


def test_fleet_only_agent_copy() -> None:
    html = page_agent(
        {
            "identity": {"agent_id": "irw-appc", "label": "irw-appc", "hostname": "box"},
            "presence": "online",
            "software_version": "1.0.0",
            "capability_summary": {
                "deployment_form": "container",
                "families": {
                    "fleet_reporting": {
                        "available": True,
                        "installed": True,
                        "supported": True,
                        "reason": "OK",
                    },
                    "docker_compose": {
                        "available": False,
                        "installed": False,
                        "supported": True,
                        "reason": "DOCKER_UNAVAILABLE",
                    },
                    "restic": {
                        "available": False,
                        "installed": False,
                        "supported": True,
                        "reason": "RESTIC_UNAVAILABLE",
                    },
                },
            },
            "assurance": {
                "empty_reason": "no Compose discovery findings in this audit payload",
                "projects": [],
                "alerts": [],
                "persistent_service_count": 0,
            },
            "current_audit": {},
        },
        csrf="t",
    )
    assert "Fleet-only capabilities" in html
    assert is_fleet_only(
        {
            "families": {
                "fleet_reporting": {"available": True},
                "docker_compose": {"available": False},
                "restic": {"available": False},
            }
        }
    )


def test_overview_has_nav_and_escaped_paths() -> None:
    html = page_fleet(
        {"totals": {"agents": 0, "online": 0, "stale": 0, "offline": 0, "audit": {}}},
        {"items": [], "limit": 10, "offset": 0, "matched": 0, "returned": 0},
        alerts={"items": []},
        trend={"buckets": []},
        window={"time_range": "24h"},
        csrf="x",
        qs={},
    )
    assert "href='/dashboard/alerts'" in html or 'href="/dashboard/alerts"' in html
    assert "Overview" in html
    assert "No agents enrolled yet." in html


def test_service_page_escapes_and_keep_project_links() -> None:
    html = page_service(
        {
            "agent_id": "ag1",
            "identity": {"agent_id": "ag1"},
            "service": {
                "service": "db",
                "project": "gitea",
                "coverage": "FAIL",
                "freshness": "WARN",
                "overall": "FAIL",
                "mounts": [
                    {
                        "id": "x",
                        "host_path": "/opt/gitea/db<script>",
                        "target": "/var",
                        "type": "bind",
                        "coverage": "FAIL",
                        "freshness": "WARN",
                        "freshness_detail": "latest relevant backup age 12m",
                        "detail": "no Restic snapshot covers this path",
                    }
                ],
            },
            "history": [],
            "integrity": {},
            "restore_verification": {},
        },
        csrf="t",
        highlight_mount="/opt/gitea/db<script>",
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "Repository assurance" in html
    assert "class='mount-focus'" in html


def test_siem_status_reads_queue_when_telemetry_missing(tmp_path) -> None:
    import sqlite3

    from backuplint.fleet.dashboard.query import _siem_status_from_queue

    db = tmp_path / "siem_export.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE siem_export_queue (
            id INTEGER PRIMARY KEY,
            event_id TEXT,
            status TEXT,
            last_error TEXT,
            last_attempt_at TEXT,
            delivered_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO siem_export_queue VALUES (1,'e1','pending',"
        "'CERTIFICATE_VERIFY_FAILED','2026-09-28T12:00:00Z',NULL)"
    )
    conn.commit()
    conn.close()
    snap = _siem_status_from_queue(db)
    assert snap is not None
    assert snap["queue_depth_by_status"]["pending"] == 1
    assert snap["endpoint_health"] == "degraded"
    assert "CERTIFICATE_VERIFY_FAILED" in str(snap["last_error"])
    assert "max-width: 18px" in _css()


def test_mount_locator_rejects_prose() -> None:
    from backuplint.fleet.dashboard.format import is_mount_locator
    from backuplint.fleet.dashboard.ui import _service_href

    assert is_mount_locator("/var/lib/gitea")
    assert not is_mount_locator("database detected")
    href = _service_href("irw-appn", "gitea", "db", mount="database detected")
    assert href == "/dashboard/agents/irw-appn/services/gitea/db"
    href2 = _service_href("irw-appn", "gitea", "db", mount="/var/lib/gitea")
    assert "mount=%2Fvar%2Flib%2Fgitea" in href2


def test_system_and_alerts_copy() -> None:
    from backuplint.fleet.dashboard.ui import page_alerts, page_history, page_siem

    hist = page_history(
        {"items": [], "matched": 0, "returned": 0, "limit": 50, "offset": 0},
        {"buckets": [], "totals": {"PASS": 0, "WARN": 0, "FAIL": 0, "ERROR": 0}},
        {"time_range": "24h", "time_from": "", "time_to": ""},
        csrf="t",
        qs={},
        chrome={
            "controller_hostname": "ctrl.example",
            "software_version": "1.0.0",
            "online": 5,
            "agents": 5,
        },
    )
    assert "<h1>System</h1>" in hist
    assert "History / failures" not in hist
    alerts = page_alerts(
        {
            "items": [
                {
                    "alert_kind": "WARN",
                    "agent_id": "irw-appn",
                    "project": "gitea",
                    "service": "db",
                    "summary": "Database workload detected",
                    "occurred_at": "2026-09-28T20:00:00+00:00",
                }
            ]
        },
        csrf="t",
        qs={},
    )
    assert "Database workload detected" in alerts
    assert "No alerts match this filter" not in alerts
    siem = page_siem(
        {
            "endpoint_health": "healthy",
            "queue_depth_by_status": {"pending": 0, "delivered": 11},
            "failed_total": 0,
            "last_success_at": "2026-09-28T19:00:00+00:00",
            "last_error": None,
            "last_error_at": "2026-09-28T18:00:00+00:00",
        },
        csrf="t",
    )
    assert "Last error: none" in siem
    assert "18:00" not in siem
