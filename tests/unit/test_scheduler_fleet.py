"""Scheduler → fleet agent integration tests."""

from __future__ import annotations

from pathlib import Path

from backuplint.config import parse_config_data
from backuplint.fleet.agent import FleetAgent
from backuplint.fleet.controller import FleetController
from backuplint.scheduler import Scheduler


def test_daemon_submits_to_controller(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "f").write_text("x\n", encoding="utf-8")
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n"
        "  app:\n"
        "    image: alpine:3.20\n"
        "    command: [\"true\"]\n"
        "    volumes: [\"./data:/data\"]\n",
        encoding="utf-8",
    )

    ctrl_dir = tmp_path / "controller"
    controller = FleetController(ctrl_dir, hostname="localhost")
    _host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    pending = controller.create_pending_agent(label="sched", ttl_hours=1)
    identity = FleetAgent.enroll_with_ca(
        controller_url=url,
        token=pending["token"],
        agent_id=pending["agent_id"],
        ca_cert=ctrl_dir / "ca" / "ca.crt",
        identity_dir=tmp_path / "agent",
    )

    config = tmp_path / "backuplint.yml"
    config.write_text(
        "backup_paths:\n"
        f"  - {data}\n"
        "schedule:\n"
        f"  state_dir: {tmp_path / 'sched'}\n"
        "  coverage:\n"
        "    every: 1h\n"
        "  integrity:\n"
        "    enabled: false\n"
        "    every: 1h\n"
        "  restore_verification:\n"
        "    enabled: false\n"
        "    every: 1h\n"
        "  deep_integrity:\n"
        "    enabled: false\n"
        "    every: 1h\n"
        "fleet:\n"
        f"  controller_url: {url}\n"
        f"  identity_dir: {tmp_path / 'agent'}\n",
        encoding="utf-8",
    )
    # Ensure config parses with fleet
    parsed = parse_config_data(
        {
            "backup_paths": [str(data)],
            "schedule": {"coverage": {"every": "1h"}},
            "fleet": {
                "controller_url": url,
                "identity_dir": str(tmp_path / "agent"),
            },
        },
        source_file=config,
    )
    assert parsed.fleet is not None

    try:
        sched = Scheduler.from_paths(
            compose_file=compose,
            config_path=config,
            poll_seconds=0.2,
        )
        # Cold start: unset next_run is immediately due on first tick.
        sched._recover_after_restart()
        sched._tick()
        latest = controller.store.latest_result(identity.agent_id)
        assert latest is not None
        assert latest["result"]["result"] in {"PASS", "FAIL", "WARN"}
    finally:
        controller.close()
        sched.store.close()
        sched.lock.release()
