"""Controller CLI current-result display regression."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from backuplint.cli import app
from backuplint.events import new_run_id
from backuplint.fleet.agent import AgentQueue, FleetAgent
from backuplint.fleet.controller import FleetController
from backuplint.fleet.protocol import PROTOCOL_VERSION, ResultEnvelope, new_submission_id

runner = CliRunner()


def test_controller_agent_shows_pass_not_none(tmp_path: Path) -> None:
    controller = FleetController(tmp_path / "c", hostname="localhost")
    host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    try:
        pending = controller.create_pending_agent(label="disp", ttl_hours=1)
        identity = FleetAgent.enroll_with_ca(
            controller_url=url,
            token=pending["token"],
            agent_id=pending["agent_id"],
            ca_cert=controller.ca_dir / "ca.crt",
            identity_dir=tmp_path / "agent",
        )
        agent = FleetAgent(
            controller_url=url,
            identity=identity,
            queue=AgentQueue(tmp_path / "agent" / "queue.jsonl"),
        )
        envelope = ResultEnvelope(
            agent_id=identity.agent_id,
            submission_id=new_submission_id(),
            scan_time=datetime.now(UTC).isoformat(),
            backuplint_version="0.5.0.dev0",
            platform="test",
            result={
                "result": "PASS",
                "summary": {
                    "protected": 1,
                    "warning": 0,
                    "critical": 0,
                    "skipped": 0,
                },
                "findings": [],
            },
            protocol_version=PROTOCOL_VERSION,
            run_id=new_run_id(),
        )
        agent.submit_envelope(envelope)
        result = runner.invoke(
            app,
            [
                "controller",
                "agent",
                identity.agent_id,
                "--data-dir",
                str(tmp_path / "c"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "current:    PASS" in result.output
        assert "current:    None" not in result.output
        assert "current:    none" not in result.output.lower() or "PASS" in result.output
    finally:
        controller.stop()
        controller.close()
