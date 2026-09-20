"""Fleet regression: inaccessible repository -> operational ERROR envelope."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from backuplint.events import event_from_audit_result
from backuplint.fleet.agent import AgentQueue, FleetAgent
from backuplint.fleet.controller import FleetController
from backuplint.models import Mount, MountType, ServiceMounts
from backuplint.reporting import format_operational_error_json
from backuplint.siem.event import SiemEventFamily, to_siem_event


def _fake_services(tmp_path: Path) -> list[ServiceMounts]:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return [
        ServiceMounts(
            name="app",
            mounts=(
                Mount(
                    service="app",
                    type=MountType.BIND,
                    source=str(data),
                    target="/data",
                ),
            ),
        )
    ]


def test_agent_submit_inaccessible_repo_is_operational_error_not_coverage_fail(
    tmp_path: Path,
) -> None:
    controller = FleetController(tmp_path / "controller", hostname="localhost")
    host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    try:
        pending = controller.create_pending_agent(label="eng-err", ttl_hours=1)
        agent_dir = tmp_path / "agent"
        identity = FleetAgent.enroll_with_ca(
            controller_url=url,
            token=pending["token"],
            agent_id=pending["agent_id"],
            ca_cert=controller.ca_dir / "ca.crt",
            identity_dir=agent_dir,
        )
        lab = tmp_path / "lab"
        lab.mkdir()
        (lab / "data").mkdir()
        (lab / "data" / "f.txt").write_text("x\n", encoding="utf-8")
        compose = lab / "compose.yml"
        compose.write_text("services: {}\n", encoding="utf-8")
        pw = lab / "pw"
        pw.write_text("x\n", encoding="utf-8")
        pw.chmod(0o600)
        cfg = lab / "backuplint.yml"
        cfg.write_text(
            "backup_paths:\n  - ./data\n"
            f"restic:\n  repository: {tmp_path / 'no-such-repo'}\n"
            f"  password_file: {pw}\n",
            encoding="utf-8",
        )
        agent = FleetAgent(
            controller_url=url,
            identity=identity,
            queue=AgentQueue(agent_dir / "queue.jsonl"),
        )
        with patch(
            "backuplint.audit.discover_mounts",
            return_value=_fake_services(lab),
        ):
            envelope = agent.run_check_and_submit(
                compose_file=compose,
                config_path=cfg,
            )
        assert envelope.result["result"] == "ERROR"
        op = envelope.result["operational_error"]
        assert op["kind"] in {
            "repository_unavailable",
            "runtime",
            "authentication",
            "network",
        }
        assert envelope.result["summary"]["critical"] == 0
        assert envelope.result["findings"] == []

        current = controller.store.current_result_by_occurred_at(identity.agent_id)
        assert current is not None
        assert current["result"]["result"] == "ERROR"
        assert "operational_error" in current["result"]

        canon = event_from_audit_result(
            envelope.result,
            occurred_at=envelope.scan_time,
            agent_id=identity.agent_id,
            submission_id=envelope.submission_id,
            run_id=envelope.run_id,
        )
        siem = to_siem_event(canon, source_role="controller")
        assert siem is not None
        assert siem.event_family is SiemEventFamily.REPOSITORY_UNAVAILABLE
    finally:
        controller.stop()
        controller.close()


def test_format_operational_error_stable_shape() -> None:
    raw = format_operational_error_json(
        message="Unable to open Restic repository (inaccessible or missing).",
        kind="repository_unavailable",
    )
    data = json.loads(raw)
    assert data["result"] == "ERROR"
    assert data["operational_error"]["engine"] == "restic"
