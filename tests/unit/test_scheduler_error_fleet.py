"""Daemon scheduled operational ERROR is submitted to the fleet controller."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from backuplint.config import parse_config_data
from backuplint.fleet.agent import FleetAgent
from backuplint.fleet.controller import FleetController
from backuplint.restic import ResticError
from backuplint.schedule_config import ScheduleCheckType
from backuplint.scheduler import Scheduler, run_scheduled_check


def _stack(tmp_path: Path) -> tuple[Path, Path]:
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
    return compose, data


def _enable_jobs(tmp_path: Path, url: str, extra: str) -> Path:
    config = tmp_path / "backuplint.yml"
    config.write_text(
        "backup_paths:\n"
        f"  - {tmp_path / 'data'}\n"
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
        f"  identity_dir: {tmp_path / 'agent'}\n"
        f"{extra}",
        encoding="utf-8",
    )
    return config


def test_daemon_submits_scheduled_restic_error(tmp_path: Path) -> None:
    compose, _data = _stack(tmp_path)
    controller = FleetController(tmp_path / "controller", hostname="localhost")
    _host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    pending = controller.create_pending_agent(label="sched-err", ttl_hours=1)
    identity = FleetAgent.enroll_with_ca(
        controller_url=url,
        token=pending["token"],
        agent_id=pending["agent_id"],
        ca_cert=tmp_path / "controller" / "ca" / "ca.crt",
        identity_dir=tmp_path / "agent",
    )
    pw = tmp_path / "pw"
    pw.write_text("secret\n", encoding="utf-8")
    pw.chmod(0o600)
    config = _enable_jobs(
        tmp_path,
        url,
        extra=(
            "restic:\n"
            f"  repository: {tmp_path / 'missing-repo'}\n"
            f"  password_file: {pw}\n"
        ),
    )
    parse_config_data(
        {
            "backup_paths": [str(tmp_path / "data")],
            "schedule": {"coverage": {"every": "1h"}},
            "fleet": {
                "controller_url": url,
                "identity_dir": str(tmp_path / "agent"),
            },
        },
        source_file=config,
    )
    try:
        sched = Scheduler.from_paths(
            compose_file=compose,
            config_path=config,
            poll_seconds=0.2,
        )
        sched._recover_after_restart()
        sched._tick()
        latest = controller.store.latest_result(identity.agent_id)
        assert latest is not None
        assert latest["result"]["result"] == "ERROR"
        assert "operational_error" in latest["result"]
        assert latest["result"]["summary"]["critical"] == 0
        # One tick -> one stored audit result (no duplicate envelope).
        rows = controller.store._conn.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM events WHERE agent_id = ?",
            (identity.agent_id,),
        ).fetchone()
        assert int(rows[0]) == 1
    finally:
        controller.close()
        sched.store.close()
        sched.lock.release()


def test_scheduled_error_queues_when_controller_down(tmp_path: Path) -> None:
    compose, _data = _stack(tmp_path)
    identity_dir = tmp_path / "agent"
    identity_dir.mkdir()
    # Minimal identity files are created by enroll; here we only need queue path.
    config = _enable_jobs(
        tmp_path,
        "https://127.0.0.1:1",
        extra="",
    )
    sched = Scheduler.from_paths(
        compose_file=compose,
        config_path=config,
        poll_seconds=0.2,
    )
    try:
        with patch(
            "backuplint.scheduler.run_scheduled_check",
            return_value=(
                "ERROR",
                2,
                "Unable to open Restic repository (backend unavailable or network failure).",
                None,
            ),
        ):
            sched._maybe_submit_fleet_error(
                "Unable to open Restic repository (backend unavailable or network failure).",
                scanned_at=sched._now(),
            )
        queue = identity_dir / "queue.jsonl"
        # Identity load fails without enroll; queue may be empty — that is still
        # non-crashing standalone-safe behavior. Enroll then retry.
    finally:
        sched.store.close()
        sched.lock.release()

    controller = FleetController(tmp_path / "controller", hostname="localhost")
    _host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    pending = controller.create_pending_agent(label="q", ttl_hours=1)
    FleetAgent.enroll_with_ca(
        controller_url=url,
        token=pending["token"],
        agent_id=pending["agent_id"],
        ca_cert=tmp_path / "controller" / "ca" / "ca.crt",
        identity_dir=identity_dir,
    )
    controller.stop()
    controller.close()
    config.write_text(
        config.read_text(encoding="utf-8").replace("https://127.0.0.1:1", url),
        encoding="utf-8",
    )
    sched2 = Scheduler.from_paths(
        compose_file=compose,
        config_path=config,
        poll_seconds=0.2,
    )
    try:
        sched2._maybe_submit_fleet_error(
            "Unable to open Restic repository (backend unavailable or network failure).",
            scanned_at=sched2._now(),
        )
        queue = identity_dir / "queue.jsonl"
        assert queue.exists()
        assert queue.read_text(encoding="utf-8").strip()
        assert "ERROR" in queue.read_text(encoding="utf-8")
    finally:
        sched2.store.close()
        sched2.lock.release()


def test_standalone_error_does_not_require_controller(tmp_path: Path) -> None:
    compose, data = _stack(tmp_path)
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
        "    every: 1h\n",
        encoding="utf-8",
    )
    result, code, _detail, outcome = run_scheduled_check(
        compose_file=compose,
        config_path=config,
        check_type=ScheduleCheckType.COVERAGE,
    )
    assert result in {"PASS", "FAIL", "WARN"}
    assert outcome is not None
    with patch(
        "backuplint.scheduler.run_audit",
        side_effect=ResticError(
            "Unable to open Restic repository (repository is locked)."
        ),
    ):
        result, code, detail, outcome = run_scheduled_check(
            compose_file=compose,
            config_path=config,
            check_type=ScheduleCheckType.COVERAGE,
        )
    assert result == "ERROR"
    assert code == 2
    assert outcome is None
    assert "locked" in (detail or "").lower()
