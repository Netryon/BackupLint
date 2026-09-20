"""Integration tests for controller SIEM export wiring."""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backuplint.fleet.controller import FleetController
from backuplint.fleet.protocol import ResultEnvelope, new_submission_id
from backuplint.secrets import SecretRef, SecretSource
from backuplint.siem.config import SiemAuthType, SiemConfig
from backuplint.siem.transport.base import TransportResult
from tests.helpers.fleet_lab import enroll_test_agent


class RecordingTransport:
    name = "recording"

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    def send(self, event, *, config: SiemConfig) -> TransportResult:
        self.sent.append(event.event_id)
        if self.fail:
            return TransportResult(
                delivered=False,
                retryable=True,
                status_code=503,
                retry_after_seconds=None,
                error="unavailable",
            )
        return TransportResult(True, False, 200, None, None)


def _siem_config(endpoint: str = "https://siem.example.internal/ingest") -> SiemConfig:
    return SiemConfig(
        enabled=True,
        endpoint=endpoint,
        auth_type=SiemAuthType.BEARER,
        auth_token=SecretRef(source=SecretSource.ENV, name="BACKUPLINT_SIEM_TOKEN"),
        tls_verify=False,
    )


def _fail_envelope(agent_id: str) -> ResultEnvelope:
    return ResultEnvelope(
        agent_id=agent_id,
        submission_id=new_submission_id(),
        scan_time=datetime.now(UTC).isoformat(),
        backuplint_version="0.7.test",
        platform="test",
        result={"summary": {"result": "FAIL"}, "reason": "lab failure"},
    )


def test_controller_ingest_exports_to_siem_when_enabled(tmp_path: Path) -> None:
    controller = FleetController(
        tmp_path / "controller",
        hostname="localhost",
        siem=_siem_config(),
    )
    transport = RecordingTransport()
    assert controller._siem_exporter is not None
    controller._siem_exporter.transport = transport
    host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    try:
        agent = enroll_test_agent(controller, tmp_path, url)
        envelope = _fail_envelope(agent.identity.agent_id)
        agent.submit_envelope(envelope)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not transport.sent:
            time.sleep(0.05)
        assert transport.sent, "expected background exporter delivery"
        assert controller._siem_exporter.queue.depth_by_status()["delivered"] >= 1
    finally:
        controller.close()


def test_controller_ingest_survives_siem_outage(tmp_path: Path) -> None:
    controller = FleetController(
        tmp_path / "controller",
        hostname="localhost",
        siem=_siem_config(),
    )
    transport = RecordingTransport(fail=True)
    assert controller._siem_exporter is not None
    controller._siem_exporter.transport = transport
    host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    try:
        agent = enroll_test_agent(controller, tmp_path, url)
        envelope = _fail_envelope(agent.identity.agent_id)
        agent.submit_envelope(envelope)
        time.sleep(0.5)
        depth = controller._siem_exporter.queue.depth_by_status()
        assert depth.get("pending", 0) >= 1 or depth.get("in_flight", 0) >= 1
        transport.fail = False
        controller._siem_exporter.drain_once(wall_clock_budget_seconds=5.0)
        assert transport.sent
    finally:
        controller.close()


def test_siem_status_requires_mtls(tmp_path: Path) -> None:
    controller = FleetController(
        tmp_path / "controller",
        hostname="localhost",
        siem=_siem_config(),
    )
    host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    try:
        anon = ssl.create_default_context()
        anon.check_hostname = False
        anon.verify_mode = ssl.CERT_NONE
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(  # noqa: S310
                url + "/v1/siem/status", context=anon, timeout=5
            )
        assert caught.value.code == 401

        agent = enroll_test_agent(controller, tmp_path, url)
        req = urllib.request.Request(url + "/v1/siem/status", method="GET")  # noqa: S310
        with urllib.request.urlopen(  # noqa: S310
            req, context=agent._ssl_context(), timeout=5
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["ok"] is True
        assert payload["siem"]["enabled"] is True
        assert "endpoint_health" in payload["siem"]
    finally:
        controller.close()


def test_all_in_one_skips_local_siem_when_fleet_configured() -> None:
    from backuplint.config import BackupLintConfig
    from backuplint.fleet_config import FleetConfig
    from backuplint.siem.runtime import should_use_local_siem_feed

    cfg = BackupLintConfig(
        backup_paths=(),
        fleet=FleetConfig(
            controller_url="https://controller.example:8443",
            identity_dir=Path("/tmp/agent"),
        ),
        siem=_siem_config(),
    )
    assert should_use_local_siem_feed(cfg.siem, has_fleet=True) is False
