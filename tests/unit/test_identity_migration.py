"""Stable identity and controller migration tests."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from backuplint.events import EventType
from backuplint.fleet.controller_store import ControllerStore
from backuplint.fleet.protocol import ResultEnvelope, new_submission_id


def _seed_v1_db(path: Path) -> None:
    """Create a pre-stabilization controller DB (no events table / meta)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE enroll_tokens (
          token_hash TEXT PRIMARY KEY,
          label TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          redeemed_at TEXT,
          created_at TEXT NOT NULL
        );
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
        CREATE TABLE heartbeats (
          agent_id TEXT PRIMARY KEY,
          last_heartbeat TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO agents (
          agent_id, label, hostname, status, first_seen, last_seen, protocol_version
        ) VALUES (?, ?, ?, 'active', ?, ?, 1)
        """,
        (
            "agent-oldstable01",
            "web1",
            "server-prod-01",
            "2026-09-01T00:00:00+00:00",
            "2026-09-01T00:00:00+00:00",
        ),
    )
    sub = "11111111-1111-1111-1111-111111111111"
    conn.execute(
        """
        INSERT INTO submissions (
          submission_id, agent_id, scan_time, received_time,
          backuplint_version, platform, result_json, protocol_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (
            sub,
            "agent-oldstable01",
            "2026-09-01T01:00:00+00:00",
            "2026-09-01T01:00:05+00:00",
            "0.5.0.dev0",
            "linux",
            json.dumps({"summary": {"result": "PASS"}}),
        ),
    )
    conn.commit()
    conn.close()


def test_migrate_preserves_agents_and_history(tmp_path: Path) -> None:
    db = tmp_path / "controller.sqlite3"
    _seed_v1_db(db)
    store = ControllerStore(db)
    try:
        agents = store.list_agents()
        assert len(agents) == 1
        assert agents[0].agent_id == "agent-oldstable01"
        assert agents[0].hostname == "server-prod-01"
        latest = store.latest_result("agent-oldstable01")
        assert latest is not None
        assert latest["result"]["summary"]["result"] == "PASS"
        event = store.latest_event("agent-oldstable01")
        assert event is not None
        assert event["event_type"] == EventType.AUDIT_COMPLETED.value
        assert event["occurred_at"] == "2026-09-01T01:00:00+00:00"
        assert event["received_at"] == "2026-09-01T01:00:05+00:00"
        assert str(event["run_id"]).startswith("legacy-")
    finally:
        store.close()


def test_hostname_and_label_rename_keep_agent_id(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "c.sqlite3")
    try:
        store.register_agent(
            agent_id="agent-stableid01",
            label="web1",
            hostname="server-prod-01",
        )
        store.set_agent_hostname("agent-stableid01", "database-primary")
        store.set_agent_label("agent-stableid01", "db-primary")
        agent = store.get_agent("agent-stableid01")
        assert agent is not None
        assert agent.agent_id == "agent-stableid01"
        assert agent.hostname == "database-primary"
        assert agent.label == "db-primary"
        assert len(store.list_agents()) == 1
    finally:
        store.close()


def test_duplicate_submission_idempotent(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "c.sqlite3")
    try:
        store.register_agent(
            agent_id="agent-stableid02",
            label="a",
            hostname="h",
        )
        sub = new_submission_id()
        env = ResultEnvelope(
            agent_id="agent-stableid02",
            submission_id=sub,
            scan_time=datetime.now(UTC).isoformat(),
            backuplint_version="0.5.0.dev0",
            platform="test",
            result={"summary": {"result": "PASS"}},
            run_id="run-" + "a" * 32,
        )
        assert store.ingest_result(env) is True
        assert store.ingest_result(env) is False
        assert store.latest_event("agent-stableid02") is not None
    finally:
        store.close()


def test_cloned_identity_same_agent_id_not_two_hosts(tmp_path: Path) -> None:
    """Same agent_id remains one registry row; clone must re-enroll for a new ID."""
    store = ControllerStore(tmp_path / "c.sqlite3")
    try:
        store.register_agent(
            agent_id="agent-cloned0001",
            label="original",
            hostname="host-a",
        )
        # A cloned identity presenting the same agent_id updates hostname only if
        # explicitly set — it must not create a second agent row.
        store.set_agent_hostname("agent-cloned0001", "host-b")
        assert len(store.list_agents()) == 1
        assert store.get_agent("agent-cloned0001") is not None
    finally:
        store.close()
