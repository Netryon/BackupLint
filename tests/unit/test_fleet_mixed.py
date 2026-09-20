"""Multi-agent fleet mixed-state lab."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from backuplint.fleet.agent import AgentQueue, FleetAgent
from backuplint.fleet.controller import FleetController
from backuplint.fleet.protocol import ResultEnvelope, new_submission_id


def test_mixed_fleet_pass_fail_offline(tmp_path: Path) -> None:
    data_dir = tmp_path / "controller"
    controller = FleetController(data_dir, hostname="localhost")
    _host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    agents = []
    try:
        for label, result in (("web", "PASS"), ("db", "FAIL"), ("cache", "PASS")):
            pending = controller.create_pending_agent(label=label, ttl_hours=1)
            identity = FleetAgent.enroll_with_ca(
                controller_url=url,
                token=pending["token"],
                agent_id=pending["agent_id"],
                ca_cert=data_dir / "ca" / "ca.crt",
                identity_dir=tmp_path / label,
                hostname=label,
            )
            agent = FleetAgent(
                controller_url=url,
                identity=identity,
                queue=AgentQueue(tmp_path / label / "q.jsonl"),
            )
            agent.heartbeat()
            agent.submit_envelope(
                ResultEnvelope(
                    agent_id=identity.agent_id,
                    submission_id=new_submission_id(),
                    scan_time=datetime.now(UTC).isoformat(),
                    backuplint_version="0.5.0.dev0",
                    platform="test",
                    result={"summary": {"result": result}},
                )
            )
            agents.append((label, identity, agent, result))

        # Revoke one agent — old PASS must not be treated as current auth.
        revoked_id = agents[2][1].agent_id
        controller.store.set_agent_status(revoked_id, "revoked")
        try:
            agents[2][2].heartbeat()
            raised = False
        except Exception:
            raised = True
        assert raised

        listed = {a.agent_id: a for a in controller.store.list_agents()}
        assert listed[agents[0][1].agent_id].status == "active"
        assert listed[agents[1][1].agent_id].status == "active"
        assert listed[revoked_id].status == "revoked"
        assert (
            controller.store.latest_result(agents[0][1].agent_id)["result"]["summary"][
                "result"
            ]
            == "PASS"
        )
        assert (
            controller.store.latest_result(agents[1][1].agent_id)["result"]["summary"][
                "result"
            ]
            == "FAIL"
        )
    finally:
        controller.close()
