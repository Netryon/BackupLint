"""Dashboard project-aware service URLs and alert deep-links."""

from __future__ import annotations

from backuplint.fleet.dashboard.ui import _service_href, page_fleet, page_service


def test_service_href_includes_project_and_mount() -> None:
    href = _service_href(
        "agent-a",
        "gitea",
        "db",
        mount="/opt/matrix/gitea/db",
    )
    assert href.startswith("/dashboard/agents/agent-a/services/gitea/db")
    assert "mount=" in href
    other = _service_href("agent-a", "nextcloud", "db")
    assert "/services/nextcloud/db" in other
    assert "/services/gitea/db" not in other


def test_page_service_highlights_requested_mount() -> None:
    html = page_service(
        {
            "agent_id": "ag1",
            "identity": {"agent_id": "ag1"},
            "service": {
                "service": "db",
                "project": "gitea",
                "image": "postgres:16-alpine",
                "coverage": "FAIL",
                "freshness": "NOT_APPLICABLE",
                "integrity": "NOT_RUN",
                "restore": "NOT_RUN",
                "overall": "FAIL",
                "mounts": [
                    {
                        "id": "db:/opt/gitea/db",
                        "host_path": "/opt/gitea/db",
                        "target": "/var/lib/postgresql/data",
                        "type": "bind",
                        "storage_class": "persistent",
                        "expected_backed_up": True,
                        "coverage": "FAIL",
                        "freshness": "NOT_APPLICABLE",
                        "integrity": "NOT_RUN",
                        "restore": "NOT_RUN",
                        "integrity_scope": "repository",
                        "restore_scope": "repository",
                        "detail": "no Restic snapshot covers this path",
                    },
                    {
                        "id": "db:/opt/gitea/other",
                        "host_path": "/opt/gitea/other",
                        "target": "/extra",
                        "type": "bind",
                        "storage_class": "persistent",
                        "expected_backed_up": True,
                        "coverage": "PASS",
                        "freshness": "PASS",
                        "integrity": "NOT_RUN",
                        "restore": "NOT_RUN",
                        "integrity_scope": "repository",
                        "restore_scope": "repository",
                        "detail": "covered",
                    },
                ],
            },
            "history": [],
            "integrity": {},
            "restore_verification": {},
        },
        csrf="token",
        highlight_mount="/opt/gitea/db",
    )
    assert "class='mount-focus'" in html
    assert html.count("class='mount-focus'") == 1
    assert "/opt/gitea/db" in html
    assert "/opt/gitea/other" in html


def test_overview_alerts_deep_link_project_service() -> None:
    html = page_fleet(
        {
            "totals": {
                "agents": 1,
                "online": 1,
                "stale": 0,
                "offline": 0,
                "audit_pass": 0,
                "audit_fail": 1,
                "audit_error": 0,
                "audit_warn": 0,
                "persistent_services": 2,
                "healthy_persistent_services": 1,
                "failing_persistent_services": 1,
                "assurance_alerts": 1,
            },
            "presence": {},
            "audit": {},
        },
        {
            "items": [
                {
                    "agent_id": "agent-a",
                    "identity": {"label": "agent-a", "hostname": "agent-a"},
                    "presence": "online",
                    "current_audit": {"status": "FAIL"},
                    "assurance": {
                        "persistent_service_count": 2,
                        "healthy_count": 1,
                        "failing_count": 1,
                        "alert_count": 1,
                    },
                }
            ],
            "limit": 10,
            "offset": 0,
            "total": 1,
        },
        alerts={
            "items": [
                {
                    "occurred_at": "2026-09-22T17:00:00+00:00",
                    "agent_id": "agent-a",
                    "alert_kind": "FAIL",
                    "status": "FAIL",
                    "project": "gitea",
                    "service": "db",
                    "mount": "/opt/gitea/db",
                    "summary": "coverage: db /opt/gitea/db uncovered",
                }
            ]
        },
        trend={"buckets": []},
        window={"time_range": "24h", "time_from": "a", "time_to": "b"},
        csrf="x",
        qs={},
    )
    assert "/dashboard/agents/agent-a/services/gitea/db" in html
    assert "mount=" in html
