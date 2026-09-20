#!/usr/bin/env python3
"""Disposable upgrade + controller recovery exercise for pre-v1 closure.

Uses only disposable copies under --work-dir. Never touches preserved evidence.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backuplint.fleet.agent import AgentQueue, FleetAgent
from backuplint.fleet.controller import FleetController
from backuplint.fleet.controller_store import STORE_SCHEMA_VERSION, ControllerStore
from backuplint.fleet.dashboard.config import DashboardConfig
from backuplint.fleet.protocol import ResultEnvelope, new_submission_id
from backuplint.secrets import SecretRef, SecretSource
from backuplint.siem.config import SiemAuthType, SiemConfig
from backuplint.sqlite_backup import restore_sqlite_backup_file
from tests.helpers.persistence_lab import seed_controller_v1_db


def _siem() -> SiemConfig:
    return SiemConfig(
        enabled=True,
        endpoint="https://siem.example.internal/ingest",
        auth_type=SiemAuthType.BEARER,
        auth_token=SecretRef(source=SecretSource.ENV, name="BACKUPLINT_SIEM_TOKEN"),
        tls_verify=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    import os

    os.environ.setdefault("BACKUPLINT_SIEM_TOKEN", "recovery-exercise-token")

    work = Path(args.work_dir or tempfile.mkdtemp(prefix="bl-pre-v1-recovery-"))
    work.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    ok = True

    # --- Upgrade from older seeded controller DB ---
    old_db = work / "upgrade" / "controller.sqlite3"
    old_db.parent.mkdir(parents=True, exist_ok=True)
    seed_controller_v1_db(old_db)
    upgraded = ControllerStore(old_db)
    ver = upgraded.schema_version()
    notes.append(f"upgrade schema_version={ver} expected={STORE_SCHEMA_VERSION}")
    if ver != STORE_SCHEMA_VERSION:
        ok = False
    diag = upgraded.diagnostics()
    notes.append(f"upgrade integrity_ok={diag.integrity_ok}")
    if not diag.integrity_ok:
        ok = False
    upgraded.close()

    # --- Live controller with policy + SIEM + dashboard ---
    data = work / "live"
    ctrl = FleetController(
        data,
        hostname="127.0.0.1",
        dashboard=DashboardConfig(enabled=True, cookie_secure=False),
        siem=_siem(),
    )
    ctrl.dashboard_auth.set_password("recovery-pass-ok")
    host, port = ctrl.start(host="127.0.0.1", port=0)
    url = f"https://{host}:{port}"
    ca = ctrl.ca_dir / "ca.crt"
    snap = ctrl.store.policy_create(
        display_name="recovery-policy",
        description="",
        settings={"reporting": {"policy_poll_interval": "5m"}},
        created_by="recovery",
    )
    pending = ctrl.create_pending_agent(label="recovery-agent", ttl_hours=1)
    identity = FleetAgent.enroll_with_ca(
        controller_url=url,
        token=pending["token"],
        agent_id=pending["agent_id"],
        ca_cert=ca,
        identity_dir=work / "agent",
        hostname="recovery-host",
    )
    ctrl.store.policy_assign_agent(identity.agent_id, snap.revision_id)
    agent = FleetAgent(
        controller_url=url,
        identity=identity,
        queue=AgentQueue(work / "agent" / "q.jsonl"),
        sleep=lambda _s: None,
    )
    agent.heartbeat(max_attempts=3)
    agent.submit_envelope(
        ResultEnvelope(
            agent_id=identity.agent_id,
            submission_id=new_submission_id(),
            scan_time=datetime.now(UTC).isoformat(),
            backuplint_version="0.8.0.dev0",
            platform="recovery",
            result={"summary": {"result": "PASS"}},
        )
    )
    desired = agent.fetch_desired_policy(max_attempts=3)
    overview = ctrl.dashboard_query.fleet_overview()
    siem_before = ctrl.siem_status_snapshot(persist=True)
    notes.append(f"pre-backup desired_status={desired.get('status')}")
    notes.append(f"pre-backup dashboard_agents={overview.get('counts', overview)}")
    notes.append(f"pre-backup siem_enabled={siem_before.get('enabled')}")

    # Backup disposable state
    bak = work / "backup"
    bak.mkdir(parents=True, exist_ok=True)
    ctrl.store.backup(bak / "controller.sqlite3")
    for name in ("ca", "server", "dashboard", "siem"):
        src = data / name
        if src.exists():
            shutil.copytree(src, bak / name, dirs_exist_ok=True)
    notes.append("backup created for controller db + ca/server/dashboard/siem")

    # Destroy live state
    ctrl.stop()
    ctrl.close()
    shutil.rmtree(data)
    notes.append("destroyed disposable live data-dir")

    # Restore
    data.mkdir(parents=True, exist_ok=True)
    restore_sqlite_backup_file(
        bak / "controller.sqlite3",
        data / "controller.sqlite3",
        store_type="controller",
    )
    for name in ("ca", "server", "dashboard", "siem"):
        src = bak / name
        if src.exists():
            shutil.copytree(src, data / name, dirs_exist_ok=True)
    notes.append("restored disposable controller state")

    ctrl2 = FleetController(
        data,
        hostname="127.0.0.1",
        dashboard=DashboardConfig(enabled=True, cookie_secure=False),
        siem=_siem(),
    )
    host2, port2 = ctrl2.start(host="127.0.0.1", port=0)
    url2 = f"https://{host2}:{port2}"
    agent.controller_url = url2
    # Agent still has old CA; restored CA should match.
    try:
        agent.heartbeat(max_attempts=3)
        notes.append("post-restore agent heartbeat ok")
    except Exception as exc:  # noqa: BLE001
        ok = False
        notes.append(f"post-restore agent heartbeat FAILED: {exc}")
    try:
        overview2 = ctrl2.dashboard_query.fleet_overview()
        notes.append(f"post-restore dashboard ok keys={sorted(overview2.keys())[:8]}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        notes.append(f"post-restore dashboard FAILED: {exc}")
    try:
        siem_after = ctrl2.siem_status_snapshot(persist=False)
        notes.append(f"post-restore siem enabled={siem_after.get('enabled')}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        notes.append(f"post-restore siem FAILED: {exc}")
    try:
        desired2 = ctrl2.store.policy_get_desired(identity.agent_id)
        notes.append(
            f"post-restore policy status={desired2.get('status')} "
            f"rev={desired2.get('revision_id')}"
        )
        if desired2.get("revision_id") != snap.revision_id:
            ok = False
            notes.append("policy revision mismatch after restore")
        audit = ctrl2.store.policy_list_audit(limit=10)
        notes.append(f"post-restore policy_audit_rows={len(audit)}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        notes.append(f"post-restore policy FAILED: {exc}")
    diag2 = ctrl2.store.diagnostics()
    notes.append(f"post-restore integrity_ok={diag2.integrity_ok}")
    if not diag2.integrity_ok:
        ok = False

    ctrl2.stop()
    ctrl2.close()

    report = {
        "ok": ok,
        "work_dir": str(work),
        "schema_version": STORE_SCHEMA_VERSION,
        "notes": notes,
        "finished_at": datetime.now(UTC).isoformat(),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
