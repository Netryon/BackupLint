"""CSR enrollment security tests (agent-owned private key)."""

from __future__ import annotations

from pathlib import Path

import pytest

from backuplint.fleet.agent import AgentQueue, FleetAgent
from backuplint.fleet.controller import FleetController


def test_controller_never_returns_or_stores_private_key(tmp_path: Path) -> None:
    controller = FleetController(tmp_path / "c", hostname="localhost")
    host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    try:
        pending = controller.create_pending_agent(label="csr", ttl_hours=1)
        identity = FleetAgent.enroll_with_ca(
            controller_url=url,
            token=pending["token"],
            agent_id=pending["agent_id"],
            ca_cert=controller.ca_dir / "ca.crt",
            identity_dir=tmp_path / "agent",
            hostname="csr-host",
        )
        # Local key exists with restrictive mode.
        key = tmp_path / "agent" / "client.key"
        assert key.is_file()
        assert (key.stat().st_mode & 0o777) == 0o600
        # Controller agents dir must not contain this agent's private key.
        agents_dir = controller.agents_dir
        for path in agents_dir.rglob("*") if agents_dir.exists() else []:
            assert path.name != "client.key"
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="ignore")
                assert "PRIVATE KEY" not in text
        # Token hash only in DB.
        db = (tmp_path / "c" / "controller.sqlite3").read_bytes()
        assert pending["token"].encode() not in db
        pending_row = controller.store.get_pending_enrollment(pending["agent_id"])
        assert pending_row is not None
        assert pending_row["status"] == "consumed"
        assert pending_row["cert_fingerprint"]
        # Replay rejected.
        with pytest.raises(Exception, match="enrollment failed|already used"):
            FleetAgent.enroll_with_ca(
                controller_url=url,
                token=pending["token"],
                agent_id=pending["agent_id"],
                ca_cert=controller.ca_dir / "ca.crt",
                identity_dir=tmp_path / "agent-replay",
                hostname="csr-host",
            )
        _ = identity
    finally:
        controller.close()


def test_wrong_agent_id_token_pairing_rejected(tmp_path: Path) -> None:
    controller = FleetController(tmp_path / "c", hostname="localhost")
    host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    try:
        a = controller.create_pending_agent(label="a", ttl_hours=1)
        b = controller.create_pending_agent(label="b", ttl_hours=1)
        with pytest.raises(Exception, match="enrollment failed"):
            FleetAgent.enroll_with_ca(
                controller_url=url,
                token=a["token"],
                agent_id=b["agent_id"],
                ca_cert=controller.ca_dir / "ca.crt",
                identity_dir=tmp_path / "mismatch",
            )
    finally:
        controller.close()


def test_csr_with_embedded_private_key_rejected(tmp_path: Path) -> None:
    controller = FleetController(tmp_path / "c", hostname="localhost")
    host, port = controller.start(host="127.0.0.1", port=0)
    try:
        pending = controller.create_pending_agent(label="bad", ttl_hours=1)
        # Craft a payload that includes a PEM private key marker.
        bogus = (
            "-----BEGIN CERTIFICATE REQUEST-----\nMIIB\n"
            "-----END CERTIFICATE REQUEST-----\n"
            "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----\n"
        )
        with pytest.raises(Exception, match="private key|CSR|enrollment"):
            controller.enroll(
                token=pending["token"],
                hostname="h",
                agent_id=pending["agent_id"],
                csr_pem=bogus,
            )
        # Pending must remain usable (token not burned on validation failure).
        row = controller.store.get_pending_enrollment(pending["agent_id"])
        assert row is not None
        assert row["status"] == "pending"
    finally:
        controller.close()


def test_pending_survives_controller_restart(tmp_path: Path) -> None:
    data = tmp_path / "c"
    first = FleetController(data, hostname="localhost")
    pending = first.create_pending_agent(label="restart", ttl_hours=1)
    first.close()
    second = FleetController(data, hostname="localhost")
    host, port = second.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    try:
        row = second.store.get_pending_enrollment(pending["agent_id"])
        assert row is not None
        assert row["status"] == "pending"
        identity = FleetAgent.enroll_with_ca(
            controller_url=url,
            token=pending["token"],
            agent_id=pending["agent_id"],
            ca_cert=second.ca_dir / "ca.crt",
            identity_dir=tmp_path / "agent",
        )
        agent = FleetAgent(
            controller_url=url,
            identity=identity,
            queue=AgentQueue(tmp_path / "agent" / "q.jsonl"),
        )
        agent.heartbeat()
    finally:
        second.close()
