"""Fleet durability: controller outage queues results then drains."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from backuplint.fleet.agent import AgentQueue, FleetAgent
from backuplint.fleet.controller import FleetController
from backuplint.fleet.protocol import ResultEnvelope, new_submission_id


def test_queue_survives_controller_outage(tmp_path: Path) -> None:
    data_dir = tmp_path / "controller"
    controller = FleetController(data_dir, hostname="localhost")
    _host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    pending = controller.create_pending_agent(label="lab", ttl_hours=1)
    identity = FleetAgent.enroll_with_ca(
        controller_url=url,
        token=pending["token"],
        agent_id=pending["agent_id"],
        ca_cert=data_dir / "ca" / "ca.crt",
        identity_dir=tmp_path / "agent",
    )
    agent = FleetAgent(
        controller_url=url,
        identity=identity,
        queue=AgentQueue(tmp_path / "agent" / "queue.jsonl"),
    )
    controller.stop()

    envelope = ResultEnvelope(
        agent_id=identity.agent_id,
        submission_id=new_submission_id(),
        scan_time=datetime.now(UTC).isoformat(),
        backuplint_version="0.5.0.dev0",
        platform="test",
        result={"summary": {"result": "PASS"}},
    )
    try:
        agent.submit_envelope(envelope)
        raised = False
    except Exception:
        raised = True
    assert raised
    assert any(
        i.get("submission_id") == envelope.submission_id for i in agent.queue.peek_all()
    )

    # Restart controller on same port if possible; else new port and update URL.
    _host2, port2 = controller.start(host="127.0.0.1", port=0)
    agent.controller_url = f"https://127.0.0.1:{port2}"
    acked = agent.flush()
    assert acked >= 1
    assert agent.queue.peek_all() == []
    latest = controller.store.latest_result(identity.agent_id)
    assert latest is not None
    assert latest["submission_id"] == envelope.submission_id
    controller.close()
