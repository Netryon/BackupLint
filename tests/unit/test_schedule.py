"""Unit tests for v0.4 schedule config, timing, locking, and store."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from random import Random

import pytest

from backuplint.config import ConfigError, parse_config_data
from backuplint.schedule_config import ScheduleCheckType, parse_schedule
from backuplint.schedule_lock import ScheduleLock, ScheduleLockError
from backuplint.schedule_store import ScheduleStore
from backuplint.scheduler import (
    compute_next_run,
    decide_due,
    format_schedule_status,
    schedule_status_payload,
)


def test_parse_schedule_defaults_and_strict_keys() -> None:
    schedule = parse_schedule(
        {
            "coverage": {"every": "15m"},
            "integrity": {"every": "2h"},
            "jitter": "1m",
            "history_limit": 50,
        },
        config_dir=None,
    )
    assert schedule is not None
    assert schedule.job_for(ScheduleCheckType.COVERAGE).every == timedelta(minutes=15)
    assert schedule.jitter == timedelta(minutes=1)
    assert schedule.history_limit == 50
    with pytest.raises(ConfigError, match="Unknown schedule"):
        parse_schedule({"coverage": {"every": "1h"}, "shell": "rm"}, config_dir=None)


def test_parse_schedule_in_full_config() -> None:
    config = parse_config_data(
        {
            "backup_paths": ["/data"],
            "schedule": {
                "coverage": {"every": "30m"},
                "integrity": {"enabled": False, "every": "6h"},
                "restore_verification": {"every": "24h"},
                "deep_integrity": {"every": "7d"},
            },
        }
    )
    assert config.schedule is not None
    assert config.schedule.job_for(ScheduleCheckType.INTEGRITY).enabled is False


def test_typo_schedule_job_key_rejected() -> None:
    with pytest.raises(ConfigError, match="Unknown schedule.coverage"):
        parse_schedule({"coverage": {"evry": "30m"}}, config_dir=None)


def test_compute_next_run_jitter_bounds() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    rng = Random(0)  # noqa: S311 - deterministic jitter bounds test
    nxt = compute_next_run(
        now=now,
        interval=timedelta(hours=1),
        jitter=timedelta(minutes=10),
        rng=rng,
    )
    assert nxt >= now + timedelta(hours=1)
    assert nxt <= now + timedelta(hours=1, minutes=10)


def test_missed_run_collapses_to_single_catchup() -> None:
    now = datetime(2026, 1, 2, tzinfo=UTC)
    job = parse_schedule(
        {"coverage": {"every": "1h"}}, config_dir=None
    ).job_for(ScheduleCheckType.COVERAGE)
    # Three hours late — still one due decision, next is interval from now.
    decision = decide_due(
        job=job,
        stored_next=now - timedelta(hours=3),
        now=now,
        jitter=None,
    )
    assert decision.due is True
    assert decision.missed is True
    assert decision.next_run == now + timedelta(hours=1)


def test_schedule_store_history_retention(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "h.sqlite3", history_limit=3)
    now = datetime.now(UTC)
    for i in range(5):
        store.record_run(
            check_type=ScheduleCheckType.COVERAGE,
            started_at=now,
            finished_at=now,
            result="PASS" if i % 2 == 0 else "FAIL",
            exit_code=0 if i % 2 == 0 else 1,
            duration_seconds=0.1,
            next_run=now + timedelta(minutes=30),
        )
    assert len(store.history(limit=20)) == 3
    state = store.get_job_state(ScheduleCheckType.COVERAGE)
    assert state is not None
    assert state.last_success is not None
    store.close()


def test_lock_exclusive(tmp_path: Path) -> None:
    path = tmp_path / "daemon.lock"
    first = ScheduleLock(path)
    second = ScheduleLock(path)
    first.acquire()
    with pytest.raises(ScheduleLockError, match="held"):
        second.acquire(blocking=False)
    first.release()
    second.acquire(blocking=False)
    owner = second.read_owner()
    assert owner is not None
    assert owner["pid"] == os.getpid()
    second.release()


def test_status_separates_schedule_from_backup_truth(tmp_path: Path) -> None:
    schedule = parse_schedule(
        {"coverage": {"every": "30m"}, "integrity": {"every": "6h"}},
        config_dir=None,
    )
    store = ScheduleStore(tmp_path / "h.sqlite3")
    now = datetime.now(UTC)
    store.record_run(
        check_type=ScheduleCheckType.COVERAGE,
        started_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=2),
        result="PASS",
        exit_code=0,
        duration_seconds=1.0,
        next_run=now - timedelta(minutes=5),  # overdue
    )
    text = format_schedule_status(schedule=schedule, store=store, now=now)
    assert "PASS" in text
    assert "stale schedule" in text or "due" in text
    assert "not a substitute" in text
    payload = schedule_status_payload(schedule=schedule, store=store, now=now)
    assert payload["schedule"]["jobs"][0]["due"] is True
    store.close()


def test_schedule_json_roundtrip_keys(tmp_path: Path) -> None:
    schedule = parse_schedule({"coverage": {"every": "10m"}}, config_dir=None)
    store = ScheduleStore(tmp_path / "h.sqlite3")
    raw = json.dumps(schedule_status_payload(schedule=schedule, store=store))
    data = json.loads(raw)
    assert "schedule" in data
    assert "jobs" in data["schedule"]
    store.close()
