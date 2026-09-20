"""Agent policy API integration tests."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from backuplint.fleet.agent import AgentIdentity, FleetAgent
from backuplint.fleet.controller import FleetController


def _settings() -> dict[str, object]:
    return {"reporting": {"policy_poll_interval": "5m"}}


def _https_get(url: str, context: ssl.SSLContext) -> dict[str, object]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, context=context, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _https_post(url: str, context: ssl.SSLContext, body: dict[str, object]) -> dict[str, object]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, context=context, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _agent_ssl(identity: AgentIdentity) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=str(identity.ca_cert))
    ctx.load_cert_chain(certfile=str(identity.client_cert), keyfile=str(identity.client_key))
    return ctx


@pytest.fixture
def fleet_lab(tmp_path: Path) -> dict[str, object]:
    data_dir = tmp_path / "controller"
    controller = FleetController(data_dir, hostname="localhost")
    host, port = controller.start(host="127.0.0.1", port=0)
    base = f"https://127.0.0.1:{port}"
    try:
        pending = controller.create_pending_agent(label="api-agent", ttl_hours=1)
        FleetAgent.enroll_with_ca(
            controller_url=base,
            token=pending["token"],
            agent_id=pending["agent_id"],
            ca_cert=controller.ca_dir / "ca.crt",
            identity_dir=tmp_path / "agent",
            hostname="h1",
        )
        identity = AgentIdentity.load(tmp_path / "agent")
        snap = controller.store.policy_create(
            display_name="API", description="", settings=_settings(), created_by="test"
        )
        controller.store.policy_assign_agent(identity.agent_id, snap.revision_id)
        yield {
            "controller": controller,
            "base": base,
            "identity": identity,
            "snap": snap,
        }
    finally:
        controller.stop()
        controller.close()


def test_policy_desired_requires_mtls(fleet_lab: dict[str, object]) -> None:
    base = str(fleet_lab["base"])
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with pytest.raises(urllib.error.HTTPError) as exc:
        _https_get(f"{base}/v1/policy/desired", ctx)
    assert exc.value.code == 401


def test_policy_desired_and_applied(fleet_lab: dict[str, object]) -> None:
    base = str(fleet_lab["base"])
    identity = fleet_lab["identity"]
    snap = fleet_lab["snap"]
    ctx = _agent_ssl(identity)
    desired = _https_get(f"{base}/v1/policy/desired", ctx)
    assert desired["status"] == "ASSIGNED"
    generation = int(desired["assignment_generation"])
    ack = _https_post(
        f"{base}/v1/policy/applied",
        ctx,
        {
            "assignment_generation": generation,
            "revision_id": snap.revision_id,
            "content_sha256": snap.content_sha256,
            "apply_status": "success",
            "drift_status": "IN_SYNC",
        },
    )
    assert ack["ok"] is True
    no_change = _https_get(
        f"{base}/v1/policy/desired?current_assignment_generation={generation}"
        f"&current_revision_id={snap.revision_id}"
        f"&current_content_sha256={snap.content_sha256}",
        ctx,
    )
    assert no_change["status"] == "NO_CHANGE"


def test_cross_agent_isolation(fleet_lab: dict[str, object], tmp_path: Path) -> None:
    controller = fleet_lab["controller"]
    base = str(fleet_lab["base"])
    identity_a = fleet_lab["identity"]
    pending_b = controller.create_pending_agent(label="b", ttl_hours=1)
    FleetAgent.enroll_with_ca(
        controller_url=base,
        token=pending_b["token"],
        agent_id=pending_b["agent_id"],
        ca_cert=controller.ca_dir / "ca.crt",
        identity_dir=tmp_path / "agent-b",
        hostname="h2",
    )
    identity_b = AgentIdentity.load(tmp_path / "agent-b")
    snap_b = controller.store.policy_create(
        display_name="B only", description="", settings=_settings(), created_by="test"
    )
    controller.store.policy_assign_agent(identity_b.agent_id, snap_b.revision_id)
    ctx_a = _agent_ssl(identity_a)
    desired_a = _https_get(f"{base}/v1/policy/desired", ctx_a)
    assert desired_a["revision_id"] != snap_b.revision_id
