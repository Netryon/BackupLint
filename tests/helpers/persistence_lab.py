"""Reusable persistence fixtures and helpers for Agent 4 recovery tests."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


def seed_controller_v1_db(path: Path, *, with_submission: bool = True) -> str:
    """Create a pre-events controller DB (schema conceptually v1)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE agents (
          agent_id TEXT PRIMARY KEY,
          label TEXT NOT NULL,
          hostname TEXT NOT NULL,
          status TEXT NOT NULL,
          first_seen TEXT NOT NULL,
          last_seen TEXT,
          protocol_version INTEGER NOT NULL
        );
        CREATE TABLE submissions (
          submission_id TEXT PRIMARY KEY,
          agent_id TEXT NOT NULL,
          scan_time TEXT NOT NULL,
          received_time TEXT NOT NULL,
          backuplint_version TEXT NOT NULL,
          platform TEXT NOT NULL,
          result_json TEXT NOT NULL,
          protocol_version INTEGER NOT NULL
        );
        CREATE TABLE enroll_tokens (
          token_hash TEXT PRIMARY KEY,
          label TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          redeemed_at TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE heartbeats (
          agent_id TEXT PRIMARY KEY,
          last_heartbeat TEXT NOT NULL
        );
        """
    )
    agent_id = "agent-fixture0001"
    conn.execute(
        """
        INSERT INTO agents (
          agent_id, label, hostname, status, first_seen, last_seen, protocol_version
        ) VALUES (?, 'web', 'host-a', 'active', ?, ?, 1)
        """,
        (agent_id, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    submission_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    if with_submission:
        conn.execute(
            """
            INSERT INTO submissions (
              submission_id, agent_id, scan_time, received_time,
              backuplint_version, platform, result_json, protocol_version
            ) VALUES (?, ?, ?, ?, '0.5.0.dev0', 'linux', ?, 1)
            """,
            (
                submission_id,
                agent_id,
                "2026-01-01T01:00:00+00:00",
                "2026-01-01T01:00:01+00:00",
                json.dumps({"summary": {"result": "PASS"}}),
            ),
        )
    conn.commit()
    conn.close()
    return agent_id


def seed_schedule_pre_meta_db(path: Path) -> None:
    """Create a schedule DB without meta.schema_version (pre-versioned)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          check_type TEXT NOT NULL,
          started_at TEXT NOT NULL,
          finished_at TEXT NOT NULL,
          result TEXT NOT NULL,
          exit_code INTEGER NOT NULL,
          duration_seconds REAL NOT NULL,
          detail TEXT
        );
        CREATE TABLE job_state (
          check_type TEXT PRIMARY KEY,
          next_run TEXT,
          last_started TEXT,
          last_finished TEXT,
          last_result TEXT,
          last_exit_code INTEGER,
          last_success TEXT,
          last_failure TEXT,
          skipped_reason TEXT
        );
        """
    )
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO runs (
          check_type, started_at, finished_at, result, exit_code, duration_seconds, detail
        ) VALUES ('coverage', ?, ?, 'PASS', 0, 0.1, NULL)
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO job_state (
          check_type, next_run, last_started, last_finished, last_result,
          last_exit_code, last_success, last_failure, skipped_reason
        ) VALUES ('coverage', ?, ?, ?, 'PASS', 0, ?, NULL, NULL)
        """,
        (now, now, now, now),
    )
    conn.commit()
    conn.close()


def abrupt_controller_writer(db_path: Path, agent_id: str = "agent-abrupt0001") -> None:
    """Subprocess helper: write then os._exit without clean close."""
    import os

    root = Path(__file__).resolve().parents[2]
    script = f"""
import os
from datetime import UTC, datetime
from pathlib import Path
from backuplint.fleet.controller_store import ControllerStore
from backuplint.fleet.protocol import ResultEnvelope, new_submission_id

db = Path({str(db_path)!r})
store = ControllerStore(db)
store.register_agent(agent_id={agent_id!r}, label="a", hostname="h")
store.heartbeat({agent_id!r})
env = ResultEnvelope(
    agent_id={agent_id!r},
    submission_id=new_submission_id(),
    scan_time=datetime.now(UTC).isoformat(),
    backuplint_version="0.5.0.dev0",
    platform="test",
    result={{"summary": {{"result": "PASS"}}}},
)
assert store.ingest_result(env) is True
os._exit(0)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src") + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(root),
        env=env,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"abrupt writer failed rc={proc.returncode} stderr={proc.stderr}"
        )
