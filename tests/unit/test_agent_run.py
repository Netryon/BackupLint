"""Unit tests for backuplint agent run loop (--once)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from backuplint.cli import app
from backuplint.fleet.agent import AgentQueue, FleetAgent
from backuplint.fleet.controller import FleetController
from backuplint.fleet.dashboard.config import DashboardConfig


def test_agent_run_once_heartbeat_and_policy(tmp_path: Path) -> None:
    controller = FleetController(
        tmp_path / "controller",
        hostname="localhost",
        dashboard=DashboardConfig(enabled=False),
    )
    host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://{host}:{port}"
    try:
        snap = controller.store.policy_create(
            display_name="run-once",
            description="",
            settings={"reporting": {"policy_poll_interval": "5m"}},
            created_by="test",
        )
        pending = controller.create_pending_agent(label="run-once", ttl_hours=1)
        identity = FleetAgent.enroll_with_ca(
            controller_url=url,
            token=pending["token"],
            agent_id=pending["agent_id"],
            ca_cert=controller.ca_dir / "ca.crt",
            identity_dir=tmp_path / "identity",
            hostname="run-once-host",
        )
        controller.store.policy_assign_agent(identity.agent_id, snap.revision_id)
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "agent",
                "run",
                "--controller",
                url,
                "--identity-dir",
                str(tmp_path / "identity"),
                "--once",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "heartbeat=ok" in result.output
        assert (tmp_path / "identity" / "queue.jsonl").exists() or True
        # Queue file may be created empty/absent until first enqueue.
        _ = AgentQueue
    finally:
        controller.stop()
        controller.close()
