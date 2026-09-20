"""Structured capability reporting tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backuplint.fleet.agent import AgentQueue, FleetAgent
from backuplint.fleet.capabilities import (
    AUDIT_FAIL_STATUSES,
    CAPABILITIES_SCHEMA_VERSION,
    CapabilityReason,
    collect_local_capabilities,
    validate_capabilities_document,
)
from backuplint.fleet.controller import FleetController
from backuplint.fleet.controller_store import STORE_SCHEMA_VERSION, ControllerStore


def test_collect_installed_available_and_unavailable() -> None:
    doc = collect_local_capabilities(restic_available=True, docker_available=True)
    assert doc["schema_version"] == CAPABILITIES_SCHEMA_VERSION
    assert "deployment_form" in doc
    caps = doc["capabilities"]
    assert caps["fleet_reporting"]["available"] is True
    assert caps["restic"]["available"] is True
    assert caps["docker_compose"]["available"] is True

    missing = collect_local_capabilities(restic_available=False, docker_available=False)
    assert missing["capabilities"]["restic"]["available"] is False
    assert missing["capabilities"]["restic"]["reason"] == CapabilityReason.RESTIC_UNAVAILABLE
    assert missing["capabilities"]["restore_verification"]["available"] is False
    assert missing["capabilities"]["restore_verification"]["reason"] == (
        CapabilityReason.RESTIC_UNAVAILABLE
    )
    # Capability unavailability is not an audit FAIL status.
    assert missing["capabilities"]["restic"]["reason"] not in AUDIT_FAIL_STATUSES


def test_validate_preserves_unknown_capability_keys() -> None:
    raw = {
        "schema_version": 1,
        "deployment_form": "native",
        "capabilities": {
            "fleet_reporting": {
                "installed": True,
                "supported": True,
                "available": True,
                "reason": "OK",
            },
            "future_widget": {
                "installed": True,
                "supported": True,
                "available": False,
                "reason": "TEMPORARILY_UNAVAILABLE",
            },
        },
    }
    out = validate_capabilities_document(raw)
    assert "future_widget" in out["capabilities"]
    assert out["capabilities"]["future_widget"]["available"] is False


def test_validate_rejects_malformed() -> None:
    with pytest.raises(ValueError):
        validate_capabilities_document([])
    with pytest.raises(ValueError):
        validate_capabilities_document({"capabilities": "nope"})


def test_controller_stores_capabilities_separate_from_audit(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "c.sqlite3")
    try:
        assert store.schema_version() == STORE_SCHEMA_VERSION
        store.register_agent(agent_id="agent-captest01", label="lab", hostname="h1")
        caps = collect_local_capabilities(restic_available=False, docker_available=True)
        store.heartbeat(
            "agent-captest01",
            protocol_version=2,
            software_version="0.5.0.dev0",
            capabilities=caps,
        )
        agent = store.get_agent("agent-captest01")
        assert agent is not None
        assert agent.software_version == "0.5.0.dev0"
        stored = store.get_agent_capabilities("agent-captest01")
        assert stored is not None
        assert stored["capabilities"]["restic"]["available"] is False
        # No audit FAIL invented from capability reason.
        assert stored["capabilities"]["restic"]["reason"] != "FAIL"
        assert store.latest_result("agent-captest01") is None
        assert store.current_result_by_occurred_at("agent-captest01") is None
    finally:
        store.close()


def test_agent_heartbeat_reports_capabilities(tmp_path: Path) -> None:
    data_dir = tmp_path / "controller"
    controller = FleetController(data_dir, hostname="localhost")
    host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    try:
        pending = controller.create_pending_agent(label="caps", ttl_hours=1)
        identity = FleetAgent.enroll_with_ca(
            controller_url=url,
            token=pending["token"],
            agent_id=pending["agent_id"],
            ca_cert=data_dir / "ca" / "ca.crt",
            identity_dir=tmp_path / "agent",
            hostname="cap-host",
        )
        agent = FleetAgent(
            controller_url=url,
            identity=identity,
            queue=AgentQueue(tmp_path / "agent" / "queue.jsonl"),
        )
        agent.heartbeat()
        stored = controller.store.get_agent_capabilities(identity.agent_id)
        assert stored is not None
        assert stored["schema_version"] == CAPABILITIES_SCHEMA_VERSION
        rec = controller.store.get_agent(identity.agent_id)
        assert rec is not None
        assert rec.software_version is not None
        assert rec.capabilities_json is not None
        json.loads(rec.capabilities_json)
    finally:
        controller.close()


def test_older_agent_heartbeat_without_capabilities_still_works(tmp_path: Path) -> None:
    """Previous-compatible agents may omit capabilities; controller must not crash."""
    store = ControllerStore(tmp_path / "c.sqlite3")
    try:
        store.register_agent(agent_id="agent-oldcaps01", label="old", hostname="h")
        store.heartbeat("agent-oldcaps01")
        assert store.get_agent_capabilities("agent-oldcaps01") is None
    finally:
        store.close()
