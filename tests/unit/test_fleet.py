"""Unit/integration-ish tests for BackupLint fleet (v0.5)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from backuplint.fleet.agent import AgentError, AgentQueue, FleetAgent
from backuplint.fleet.controller import FleetController
from backuplint.fleet.protocol import (
    PROTOCOL_VERSION,
    ResultEnvelope,
    new_submission_id,
    parse_envelope,
)


def test_envelope_rejects_secrets_and_bad_version() -> None:
    with pytest.raises(Exception, match="protocol_version|unsupported"):
        parse_envelope(
            {
                "protocol_version": 999,
                "agent_id": "agent-abcdefgh",
                "submission_id": "x",
                "scan_time": "t",
                "backuplint_version": "0.5",
                "platform": "linux",
                "result": {},
            }
        )
    with pytest.raises(Exception, match="forbidden"):
        parse_envelope(
            {
                "protocol_version": PROTOCOL_VERSION,
                "agent_id": "agent-abcdefgh",
                "submission_id": "x",
                "scan_time": "t",
                "backuplint_version": "0.5",
                "platform": "linux",
                "result": {"password": "nope"},
            }
        )


def test_controller_enroll_submit_idempotent(tmp_path: Path) -> None:
    data_dir = tmp_path / "controller"
    controller = FleetController(data_dir, hostname="localhost")
    host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    try:
        pending = controller.create_pending_agent(label="lab", ttl_hours=1)
        # Reuse fails after redeem.
        identity = FleetAgent.enroll_with_ca(
            controller_url=url,
            token=pending["token"],
            agent_id=pending["agent_id"],
            ca_cert=data_dir / "ca" / "ca.crt",
            identity_dir=tmp_path / "agent",
            hostname="lab-host",
        )
        with pytest.raises(Exception, match="enrollment failed|already used"):
            FleetAgent.enroll_with_ca(
                controller_url=url,
                token=pending["token"],
                agent_id=pending["agent_id"],
                ca_cert=data_dir / "ca" / "ca.crt",
                identity_dir=tmp_path / "agent2",
            )

        agent = FleetAgent(
            controller_url=url,
            identity=identity,
            queue=AgentQueue(tmp_path / "agent" / "queue.jsonl"),
        )
        agent.heartbeat()
        envelope = ResultEnvelope(
            agent_id=identity.agent_id,
            submission_id=new_submission_id(),
            scan_time=datetime.now(UTC).isoformat(),
            backuplint_version="0.5.0.dev0",
            platform="test",
            result={"summary": {"result": "PASS"}},
        )
        agent.submit_envelope(envelope)
        # Replay same submission — ACK safely, store once.
        agent.submit_envelope(envelope)
        latest = controller.store.latest_result(identity.agent_id)
        assert latest is not None
        assert latest["submission_id"] == envelope.submission_id

        # Revoke blocks further heartbeats.
        controller.store.set_agent_status(identity.agent_id, "revoked")
        with pytest.raises(AgentError, match="revoked|failed"):
            agent.heartbeat()
    finally:
        controller.close()


def test_agent_queue_bounded(tmp_path: Path) -> None:
    queue = AgentQueue(tmp_path / "q.jsonl", max_items=3)
    for i in range(5):
        queue.enqueue(
            ResultEnvelope(
                agent_id="agent-abcdefgh",
                submission_id=f"sub-{i}",
                scan_time="t",
                backuplint_version="0.5",
                platform="test",
                result={"summary": {"result": "PASS"}, "i": i},
            )
        )
    items = queue.peek_all()
    assert len(items) <= 3
    # Healthy overflow becomes an explicit DATA_GAP marker (never silent drop-oldest).
    from backuplint.fleet.queue_policy import QueueItemKind, classify_queue_payload

    assert any(classify_queue_payload(i) is QueueItemKind.DATA_GAP for i in items)
