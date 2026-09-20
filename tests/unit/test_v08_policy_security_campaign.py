"""v0.8 policy security campaign (bounded, local)."""

from __future__ import annotations

import json
import ssl
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from backuplint.fleet.agent import AgentIdentity, FleetAgent
from backuplint.fleet.controller import FleetController
from backuplint.fleet.controller_store import ControllerStore
from backuplint.fleet.policy_apply import ManagedPolicyDir, PolicyApplyError, apply_policy_response
from backuplint.policy.schema import PolicyError, build_policy_snapshot


def _settings() -> dict[str, object]:
    return {"reporting": {"policy_poll_interval": "5m"}}


def _agent_ssl(identity: AgentIdentity) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=str(identity.ca_cert))
    ctx.load_cert_chain(certfile=str(identity.client_cert), keyfile=str(identity.client_key))
    return ctx


def _https_get(url: str, context: ssl.SSLContext) -> dict[str, object]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, context=context, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _https_post(url: str, context: ssl.SSLContext, body: dict[str, object]) -> dict[str, object]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, context=context, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


@pytest.fixture
def campaign(tmp_path: Path) -> dict[str, object]:
    controller = FleetController(tmp_path / "controller", hostname="localhost")
    _host, port = controller.start(host="127.0.0.1", port=0)
    base = f"https://127.0.0.1:{port}"
    pending = controller.create_pending_agent(label="sec-a", ttl_hours=1)
    FleetAgent.enroll_with_ca(
        controller_url=base,
        token=pending["token"],
        agent_id=pending["agent_id"],
        ca_cert=controller.ca_dir / "ca.crt",
        identity_dir=tmp_path / "agent-a",
        hostname="a",
    )
    identity = AgentIdentity.load(tmp_path / "agent-a")
    snap = controller.store.policy_create(
        display_name="sec", description="", settings=_settings(), created_by="op"
    )
    gen = controller.store.policy_assign_agent(identity.agent_id, snap.revision_id)
    try:
        yield {
            "controller": controller,
            "base": base,
            "identity": identity,
            "snap": snap,
            "generation": gen,
            "tmp": tmp_path,
        }
    finally:
        controller.stop()
        controller.close()


def test_unauthenticated_policy_fetch_rejected(campaign: dict[str, object]) -> None:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with pytest.raises(urllib.error.HTTPError) as exc:
        _https_get(f"{campaign['base']}/v1/policy/desired", ctx)
    assert exc.value.code == 401


def test_agent_cannot_ack_for_other_agent(campaign: dict[str, object]) -> None:
    controller = campaign["controller"]
    base = str(campaign["base"])
    tmp = campaign["tmp"]
    pending_b = controller.create_pending_agent(label="sec-b", ttl_hours=1)
    FleetAgent.enroll_with_ca(
        controller_url=base,
        token=pending_b["token"],
        agent_id=pending_b["agent_id"],
        ca_cert=controller.ca_dir / "ca.crt",
        identity_dir=tmp / "agent-b",
        hostname="b",
    )
    identity_b = AgentIdentity.load(tmp / "agent-b")
    snap = campaign["snap"]
    ctx_a = _agent_ssl(campaign["identity"])
    with pytest.raises(urllib.error.HTTPError) as exc:
        _https_post(
            f"{base}/v1/policy/applied",
            ctx_a,
            {
                "agent_id": identity_b.agent_id,
                "assignment_generation": 1,
                "revision_id": snap.revision_id,
                "content_sha256": snap.content_sha256,
                "apply_status": "success",
                "drift_status": "IN_SYNC",
            },
        )
    assert exc.value.code == 403


def test_tampered_hash_rejected_on_apply(tmp_path: Path) -> None:
    managed = ManagedPolicyDir(tmp_path / "managed")
    snap = build_policy_snapshot(
        policy_id="pol-tamp01",
        created_by="t",
        display_name="t",
        settings=_settings(),
    )
    payload = snap.to_dict()
    payload["display_name"] = "tampered"
    with pytest.raises(PolicyError):
        apply_policy_response(
            managed,
            {
                "status": "ASSIGNED",
                "assignment_generation": 1,
                "revision_id": snap.revision_id,
                "content_sha256": snap.content_sha256,
                "policy": payload,
            },
            applied_at="2026-01-01T00:00:00+00:00",
        )


def test_stale_generation_rejected(tmp_path: Path) -> None:
    managed = ManagedPolicyDir(tmp_path / "managed")
    snap = build_policy_snapshot(
        policy_id="pol-stale01",
        created_by="t",
        display_name="t",
        settings=_settings(),
    )
    apply_policy_response(
        managed,
        {
            "status": "ASSIGNED",
            "assignment_generation": 5,
            "revision_id": snap.revision_id,
            "content_sha256": snap.content_sha256,
            "policy": snap.to_dict(),
        },
        applied_at="2026-01-01T00:00:00+00:00",
    )
    with pytest.raises(PolicyApplyError, match="stale"):
        apply_policy_response(
            managed,
            {
                "status": "ASSIGNED",
                "assignment_generation": 2,
                "revision_id": snap.revision_id,
                "content_sha256": snap.content_sha256,
                "policy": snap.to_dict(),
            },
            applied_at="2026-01-01T00:01:00+00:00",
        )


def test_symlink_policy_state_fails_safe(tmp_path: Path) -> None:
    managed = ManagedPolicyDir(tmp_path / "managed")
    managed.current_path.symlink_to("/etc/hosts")
    with pytest.raises(PolicyApplyError, match="symlink"):
        managed.load_state()


def test_interrupted_write_preserves_previous(tmp_path: Path) -> None:
    managed = ManagedPolicyDir(tmp_path / "managed")
    snap1 = build_policy_snapshot(
        policy_id="pol-int01",
        created_by="t",
        display_name="v1",
        settings=_settings(),
    )
    apply_policy_response(
        managed,
        {
            "status": "ASSIGNED",
            "assignment_generation": 1,
            "revision_id": snap1.revision_id,
            "content_sha256": snap1.content_sha256,
            "policy": snap1.to_dict(),
        },
        applied_at="2026-01-01T00:00:00+00:00",
    )
    # Leave a stale temp file as if a crash occurred mid-write.
    fd, name = tempfile.mkstemp(dir=str(managed.root), prefix=".current.json.", suffix=".tmp")
    Path(name).write_text("{corrupt", encoding="utf-8")
    import os

    os.close(fd)
    state = managed.load_state()
    assert state.current is not None
    assert state.current["policy"]["revision_id"] == snap1.revision_id


def test_rollback_new_generation(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "c.sqlite3")
    store.register_agent(agent_id="agent-rb01", label="r", hostname="h")
    snap1 = store.policy_create(
        display_name="v1", description="", settings=_settings(), created_by="op"
    )
    snap2 = store.policy_create_revision(
        snap1.policy_id,
        display_name="v2",
        description="",
        settings={**_settings(), "queue": {"max_items": 10}},
        created_by="op",
    )
    g1 = store.policy_assign_agent("agent-rb01", snap1.revision_id)
    g2 = store.policy_assign_agent("agent-rb01", snap2.revision_id)
    g3 = store.policy_rollback_agent("agent-rb01", snap1.revision_id)
    assert g2 > g1
    assert g3 > g2
    desired = store.policy_get_desired("agent-rb01")
    assert desired["revision_id"] == snap1.revision_id
    assert desired["assignment_generation"] == g3


def test_audit_has_no_raw_secrets(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "c.sqlite3")
    store.policy_create(
        display_name="p",
        description="",
        settings={
            "siem": {
                "enabled": True,
                "auth_token": {"source": "env", "name": "SIEM_TOKEN"},
            }
        },
        created_by="op",
    )
    blob = json.dumps(store.policy_list_audit()).lower()
    assert "hunter2" not in blob
    assert "bearer " not in blob
