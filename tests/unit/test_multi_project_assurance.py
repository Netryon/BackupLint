"""Multi-project Compose discovery and dashboard identity."""

from __future__ import annotations

import json

from backuplint.coverage import evaluate_coverage
from backuplint.fleet.dashboard.assurance import project_assurance
from backuplint.models import Mount, MountType, ServiceMounts
from backuplint.reporting import format_audit_json


def test_same_service_name_different_projects_stay_separate() -> None:
    a = ServiceMounts(
        name="db",
        project="gitea",
        image="postgres:16-alpine",
        mounts=(
            Mount(
                service="db",
                type=MountType.BIND,
                source="/srv/gitea/db",
                target="/var/lib/postgresql/data",
            ),
        ),
    )
    b = ServiceMounts(
        name="db",
        project="miniflux",
        image="postgres:16-alpine",
        mounts=(
            Mount(
                service="db",
                type=MountType.BIND,
                source="/srv/miniflux/db",
                target="/var/lib/postgresql/data",
            ),
        ),
    )
    findings = evaluate_coverage(
        [a, b], ("/srv/gitea/db",), missing_detail="not covered"
    )
    payload = json.loads(format_audit_json(findings))
    projects = {f["project"] for f in payload["findings"] if f.get("path") != "database detected"}
    assert projects == {"gitea", "miniflux"}
    view = project_assurance(payload, agent_id="ag-busy-01")
    gitea = next(
        s
        for p in view["projects"]
        for s in p["services"]
        if s["project"] == "gitea" and s["service"] == "db"
    )
    mini = next(
        s
        for p in view["projects"]
        for s in p["services"]
        if s["project"] == "miniflux" and s["service"] == "db"
    )
    assert gitea["overall"] != "FAIL"
    assert mini["overall"] == "FAIL"
