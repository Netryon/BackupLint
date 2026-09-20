"""Shared fleet test helpers."""

from __future__ import annotations

from pathlib import Path

from backuplint.fleet.agent import AgentQueue, FleetAgent
from backuplint.fleet.controller import FleetController


def enroll_test_agent(
    controller: FleetController,
    tmp_path: Path,
    url: str,
    *,
    name: str = "agent",
    label: str | None = None,
) -> FleetAgent:
    """Create pending enrollment and CSR-enroll a lab agent."""
    pending = controller.create_pending_agent(label=label or name, ttl_hours=1)
    identity = FleetAgent.enroll_with_ca(
        controller_url=url,
        token=pending["token"],
        agent_id=pending["agent_id"],
        ca_cert=controller.ca_dir / "ca.crt",
        identity_dir=tmp_path / name,
        hostname=name,
    )
    return FleetAgent(
        controller_url=url,
        identity=identity,
        queue=AgentQueue(tmp_path / name / "queue.jsonl"),
    )
