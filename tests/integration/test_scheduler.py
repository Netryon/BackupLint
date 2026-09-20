"""Integration tests for the v0.4 local scheduler."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backuplint.schedule_config import ScheduleCheckType
from backuplint.schedule_store import ScheduleStore


@pytest.fixture
def schedule_lab(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    (data / "file.txt").write_text("x\n", encoding="utf-8")
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n"
        "  app:\n"
        "    image: alpine:3.20\n"
        "    command: [\"sleep\", \"infinity\"]\n"
        "    volumes:\n"
        "      - ./data:/data\n",
        encoding="utf-8",
    )
    state_dir = tmp_path / "sched-state"
    config = tmp_path / "backuplint.yml"
    config.write_text(
        "backup_paths:\n"
        f"  - {data}\n"
        "schedule:\n"
        f"  state_dir: {state_dir}\n"
        "  history_limit: 50\n"
        "  coverage:\n"
        "    every: 2s\n"
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
    return tmp_path


def test_daemon_runs_coverage_and_stops(schedule_lab: Path) -> None:
    compose = schedule_lab / "compose.yml"
    config = schedule_lab / "backuplint.yml"
    state_dir = schedule_lab / "sched-state"
    env = os.environ.copy()
    # Ensure editable install is used.
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "backuplint",
            "daemon",
            str(compose),
            "--config",
            str(config),
            "--poll-seconds",
            "0.5",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        deadline = time.time() + 20
        db = state_dir / "history.sqlite3"
        while time.time() < deadline:
            if db.is_file():
                store = ScheduleStore(db)
                rows = store.history(limit=5)
                store.close()
                coverage_runs = [
                    r for r in rows if r.check_type is ScheduleCheckType.COVERAGE
                ]
                if coverage_runs:
                    assert coverage_runs[0].result in {"PASS", "FAIL", "ERROR"}
                    break
            time.sleep(0.3)
        else:
            stderr = proc.stderr.read() if proc.stderr else ""
            pytest.fail(f"scheduler never recorded a coverage run; stderr={stderr!r}")
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    assert proc.returncode == 0


def test_schedule_status_cli(schedule_lab: Path) -> None:
    compose = schedule_lab / "compose.yml"
    config = schedule_lab / "backuplint.yml"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "backuplint",
            "schedule",
            "status",
            str(compose),
            "--config",
            str(config),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "BackupLint Schedule" in result.stdout
    assert "coverage" in result.stdout
