"""Fleet protocol compatibility / rolling-upgrade negotiation tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from backuplint.fleet.agent import AgentQueue, FleetAgent
from backuplint.fleet.compat import (
    CURRENT_PROTOCOL_VERSION,
    PREVIOUS_PROTOCOL_VERSION,
    ProtocolCompatCode,
    check_protocol_version,
    health_protocol_payload,
    supported_protocol_versions,
)
from backuplint.fleet.controller import FleetController
from backuplint.fleet.protocol import (
    PROTOCOL_VERSION,
    ProtocolError,
    ResultEnvelope,
    new_submission_id,
    parse_envelope,
)


def _base(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "agent_id": "agent-abcdefgh",
        "submission_id": "sub-1",
        "scan_time": "2026-01-01T00:00:00+00:00",
        "backuplint_version": "0.5.0.dev0",
        "platform": "linux",
        "result": {"summary": {"result": "PASS"}},
    }
    data.update(overrides)
    return data


def test_current_protocol_accepted() -> None:
    env = parse_envelope(_base(protocol_version=CURRENT_PROTOCOL_VERSION))
    assert env.protocol_version == CURRENT_PROTOCOL_VERSION


def test_previous_protocol_accepted() -> None:
    env = parse_envelope(_base(protocol_version=PREVIOUS_PROTOCOL_VERSION))
    assert env.protocol_version == PREVIOUS_PROTOCOL_VERSION


def test_too_old_rejected() -> None:
    with pytest.raises(ProtocolError) as exc:
        parse_envelope(_base(protocol_version=0))
    assert exc.value.code == ProtocolCompatCode.TOO_OLD


def test_future_newer_rejected() -> None:
    with pytest.raises(ProtocolError) as exc:
        parse_envelope(_base(protocol_version=CURRENT_PROTOCOL_VERSION + 1))
    assert exc.value.code == ProtocolCompatCode.TOO_NEW


def test_malformed_version_rejected() -> None:
    with pytest.raises(ProtocolError) as exc:
        parse_envelope(_base(protocol_version="2"))
    assert exc.value.code == ProtocolCompatCode.MALFORMED
    with pytest.raises(ProtocolError) as exc2:
        parse_envelope(_base(protocol_version=True))
    assert exc2.value.code == ProtocolCompatCode.MALFORMED


def test_compat_helper_supported_window() -> None:
    assert supported_protocol_versions() == (
        PREVIOUS_PROTOCOL_VERSION,
        CURRENT_PROTOCOL_VERSION,
    )
    assert check_protocol_version(CURRENT_PROTOCOL_VERSION).ok
    assert check_protocol_version(PREVIOUS_PROTOCOL_VERSION).ok


def test_health_reports_software_and_protocol() -> None:
    payload = health_protocol_payload(software_version="0.5.0.dev0")
    assert payload["ok"] is True
    assert payload["protocol_version"] == CURRENT_PROTOCOL_VERSION
    assert payload["software_version"] == "0.5.0.dev0"
    assert PREVIOUS_PROTOCOL_VERSION in payload["supported_protocol_versions"]


def test_mixed_current_and_previous_fleet(tmp_path: Path) -> None:
    data_dir = tmp_path / "controller"
    controller = FleetController(data_dir, hostname="localhost")
    host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    try:
        pending = controller.create_pending_agent(label="mixed", ttl_hours=1)
        identity = FleetAgent.enroll_with_ca(
            controller_url=url,
            token=pending["token"],
            agent_id=pending["agent_id"],
            ca_cert=data_dir / "ca" / "ca.crt",
            identity_dir=tmp_path / "agent",
            hostname="mixed-host",
        )
        agent = FleetAgent(
            controller_url=url,
            identity=identity,
            queue=AgentQueue(tmp_path / "agent" / "queue.jsonl"),
        )
        for ver in (PREVIOUS_PROTOCOL_VERSION, CURRENT_PROTOCOL_VERSION):
            env = ResultEnvelope(
                agent_id=identity.agent_id,
                submission_id=new_submission_id(),
                scan_time=datetime.now(UTC).isoformat(),
                backuplint_version="0.5.0.dev0",
                platform="linux",
                result={"summary": {"result": "PASS", "proto": ver}},
                protocol_version=ver,
            )
            resp = agent._post("/v1/results", env.to_dict())
            assert resp.get("ok") is True
            assert resp.get("created") is True
        # Future rejected with machine-readable code
        bad = ResultEnvelope(
            agent_id=identity.agent_id,
            submission_id=new_submission_id(),
            scan_time=datetime.now(UTC).isoformat(),
            backuplint_version="0.5.0.dev0",
            platform="linux",
            result={"summary": {"result": "PASS"}},
            protocol_version=CURRENT_PROTOCOL_VERSION + 1,
        )
        with pytest.raises(Exception, match="newer|unsupported|PROTOCOL"):
            agent._post("/v1/results", bad.to_dict())
    finally:
        controller.close()


def test_controller_health_endpoint_versions(tmp_path: Path) -> None:
    import json
    import ssl
    import urllib.request

    controller = FleetController(tmp_path / "c", hostname="localhost")
    host, port = controller.start(host="127.0.0.1", port=0)
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(
            f"https://127.0.0.1:{port}/v1/health", context=ctx, timeout=5
        ) as resp:
            payload = json.loads(resp.read().decode())
        assert payload["protocol_version"] == CURRENT_PROTOCOL_VERSION
        assert "software_version" in payload
        assert CURRENT_PROTOCOL_VERSION in payload["supported_protocol_versions"]
        assert PREVIOUS_PROTOCOL_VERSION in payload["supported_protocol_versions"]
    finally:
        controller.close()
