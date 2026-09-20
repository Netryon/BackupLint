"""Network-ish fault tests for fleet agent/controller."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backuplint.fleet.agent import AgentError, AgentQueue, FleetAgent
from backuplint.fleet.certs import init_ca, issue_server_cert
from backuplint.fleet.controller import FleetController
from backuplint.fleet.protocol import ResultEnvelope, new_submission_id


def test_wrong_ca_rejected(tmp_path: Path) -> None:
    data_dir = tmp_path / "controller"
    other = tmp_path / "other"
    controller = FleetController(data_dir, hostname="localhost")
    other_ctrl = FleetController(other, hostname="localhost")
    _host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    try:
        pending = controller.create_pending_agent(label="lab", ttl_hours=1)
        with pytest.raises(AgentError, match="enrollment failed"):
            FleetAgent.enroll_with_ca(
                controller_url=url,
                token=pending["token"],
                agent_id=pending["agent_id"],
                ca_cert=other / "ca" / "ca.crt",
                identity_dir=tmp_path / "agent",
            )
    finally:
        controller.close()
        other_ctrl.close()


def test_plaintext_url_rejected(tmp_path: Path) -> None:
    with pytest.raises(AgentError, match="https"):
        FleetAgent.enroll_with_ca(
            controller_url="http://127.0.0.1:8443",
            token="x",
            agent_id="agent-plaintext01",
            ca_cert=tmp_path / "missing.crt",
            identity_dir=tmp_path / "agent",
        )


def test_oversized_body_rejected(tmp_path: Path) -> None:
    data_dir = tmp_path / "controller"
    controller = FleetController(data_dir, hostname="localhost")
    _host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    try:
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
            queue=AgentQueue(tmp_path / "agent" / "q.jsonl"),
        )
        huge = {"blob": "x" * 300_000}
        envelope = ResultEnvelope(
            agent_id=identity.agent_id,
            submission_id=new_submission_id(),
            scan_time=datetime.now(UTC).isoformat(),
            backuplint_version="0.5.0.dev0",
            platform="test",
            result=huge,
        )
        with pytest.raises(AgentError, match="not acknowledged"):
            agent.submit_envelope(envelope)
        assert controller.store.latest_result(identity.agent_id) is None
    finally:
        controller.close()


def test_server_cert_includes_hostname_ip_san(tmp_path: Path) -> None:
    init_ca(tmp_path / "ca")
    _key, cert = issue_server_cert(
        tmp_path / "ca", tmp_path / "server", common_name="10.1.2.3"
    )
    text = subprocess.check_output(
        ["openssl", "x509", "-in", str(cert), "-noout", "-text"],
        text=True,
    )
    assert "IP Address:10.1.2.3" in text
    assert "IP Address:127.0.0.1" in text
