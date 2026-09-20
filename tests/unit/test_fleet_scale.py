"""Medium-scale local fleet smoke: many agents enroll and submit."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from backuplint.fleet.agent import AgentQueue, FleetAgent
from backuplint.fleet.controller import FleetController
from backuplint.fleet.protocol import ResultEnvelope, new_submission_id


def test_twelve_agents_enroll_and_submit(tmp_path: Path) -> None:
    controller = FleetController(tmp_path / "controller", hostname="localhost")
    _host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    ca = tmp_path / "controller" / "ca" / "ca.crt"
    try:
        for i in range(12):
            pending = controller.create_pending_agent(label=f"agent-{i}", ttl_hours=1)
            identity = FleetAgent.enroll_with_ca(
                controller_url=url,
                token=pending["token"],
                agent_id=pending["agent_id"],
                ca_cert=ca,
                identity_dir=tmp_path / f"agent-{i}",
            )
            agent = FleetAgent(
                controller_url=url,
                identity=identity,
                queue=AgentQueue(tmp_path / f"agent-{i}" / "q.jsonl"),
            )
            envelope = ResultEnvelope(
                agent_id=identity.agent_id,
                submission_id=new_submission_id(),
                scan_time=datetime.now(UTC).isoformat(),
                backuplint_version="0.5.0.dev0",
                platform="scale",
                result={"summary": {"result": "PASS" if i % 2 == 0 else "FAIL", "n": i}},
            )
            agent.submit_envelope(envelope)
            stored = controller.store.latest_result(identity.agent_id)
            assert stored is not None
            assert stored["result"]["summary"]["n"] == i
        agents = list(controller.store.list_agents())
        assert len(agents) == 12
    finally:
        controller.close()
