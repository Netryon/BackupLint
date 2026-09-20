"""Adversarial / auth tests for BackupLint fleet."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backuplint.fleet.agent import AgentQueue, FleetAgent
from backuplint.fleet.controller import FleetController
from backuplint.fleet.protocol import ResultEnvelope, new_submission_id


def test_reject_unauthenticated_result(tmp_path: Path) -> None:
    data_dir = tmp_path / "controller"
    controller = FleetController(data_dir, hostname="localhost")
    _host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    try:
        context = ssl.create_default_context(cafile=str(data_dir / "ca" / "ca.crt"))
        body = json.dumps(
            {
                "protocol_version": 1,
                "agent_id": "agent-abcdefgh",
                "submission_id": "x",
                "scan_time": "t",
                "backuplint_version": "0.5",
                "platform": "t",
                "result": {},
            }
        ).encode()
        req = urllib.request.Request(
            url + "/v1/results",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(req, context=context, timeout=10)
        assert excinfo.value.code == 401
    finally:
        controller.close()


def test_reject_agent_impersonation(tmp_path: Path) -> None:
    data_dir = tmp_path / "controller"
    controller = FleetController(data_dir, hostname="localhost")
    _host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    try:
        pending_t1 = controller.create_pending_agent(label="a1", ttl_hours=1)
        pending_t2 = controller.create_pending_agent(label="a2", ttl_hours=1)
        a1 = FleetAgent.enroll_with_ca(
            controller_url=url,
            token=pending_t1["token"],
            agent_id=pending_t1["agent_id"],
            ca_cert=data_dir / "ca" / "ca.crt",
            identity_dir=tmp_path / "a1",
        )
        a2 = FleetAgent.enroll_with_ca(
            controller_url=url,
            token=pending_t2["token"],
            agent_id=pending_t2["agent_id"],
            ca_cert=data_dir / "ca" / "ca.crt",
            identity_dir=tmp_path / "a2",
        )
        agent = FleetAgent(
            controller_url=url,
            identity=a1,
            queue=AgentQueue(tmp_path / "a1" / "q.jsonl"),
        )
        # Claim to be a2 while presenting a1 cert.
        envelope = ResultEnvelope(
            agent_id=a2.agent_id,
            submission_id=new_submission_id(),
            scan_time=datetime.now(UTC).isoformat(),
            backuplint_version="0.5.0.dev0",
            platform="test",
            result={"summary": {"result": "PASS"}},
        )
        with pytest.raises(Exception, match="not acknowledged|403|failed|match"):
            agent.submit_envelope(envelope)
        assert controller.store.latest_result(a2.agent_id) is None
        assert controller.store.latest_result(a1.agent_id) is None
    finally:
        controller.close()


def test_invalid_token_rejected(tmp_path: Path) -> None:
    data_dir = tmp_path / "controller"
    controller = FleetController(data_dir, hostname="localhost")
    _host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    try:
        with pytest.raises(Exception, match="enrollment failed"):
            FleetAgent.enroll_with_ca(
                controller_url=url,
                token="not-a-real-token",
                agent_id="agent-doesnotexist01",
                ca_cert=data_dir / "ca" / "ca.crt",
                identity_dir=tmp_path / "bad",
            )
    finally:
        controller.close()
