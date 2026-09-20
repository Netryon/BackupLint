"""Tests for versioned canonical events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backuplint.events import (
    EVENT_SCHEMA_VERSION,
    CanonicalEvent,
    EventError,
    EventType,
    event_from_audit_result,
    event_is_stale,
    legacy_run_id,
    new_event_id,
    new_run_id,
    parse_event,
)


def test_round_trip_event() -> None:
    event = event_from_audit_result(
        {"summary": {"result": "PASS"}},
        occurred_at="2026-09-10T12:00:00+00:00",
        agent_id="agent-abcdefgh",
        submission_id="sub-1",
        received_at="2026-09-10T12:00:01+00:00",
        run_id=new_run_id(),
    )
    parsed = parse_event(event.to_dict())
    assert parsed == event
    assert parsed.schema_version == EVENT_SCHEMA_VERSION
    assert parsed.event_type == EventType.AUDIT_COMPLETED.value


def test_missing_and_unsupported_schema_version() -> None:
    base = event_from_audit_result(
        {"summary": {"result": "FAIL"}},
        occurred_at="2026-09-10T12:00:00+00:00",
        run_id=new_run_id(),
    ).to_dict()
    missing = dict(base)
    del missing["schema_version"]
    with pytest.raises(EventError, match="schema_version"):
        parse_event(missing)
    future = dict(base)
    future["schema_version"] = 99
    with pytest.raises(EventError, match="unsupported"):
        parse_event(future)
    bad = dict(base)
    bad["schema_version"] = "1"
    with pytest.raises(EventError, match="integer"):
        parse_event(bad)


def test_secret_keys_rejected() -> None:
    with pytest.raises(EventError, match="forbidden"):
        event_from_audit_result(
            {"password": "x", "summary": {"result": "PASS"}},
            occurred_at="2026-09-10T12:00:00+00:00",
            run_id=new_run_id(),
        )


def test_run_correlation_and_ids() -> None:
    run = new_run_id()
    a = event_from_audit_result(
        {"summary": {"result": "PASS"}},
        occurred_at="2026-09-10T12:00:00+00:00",
        run_id=run,
        event_id=new_event_id(),
    )
    b = event_from_audit_result(
        {"summary": {"result": "FAIL"}},
        occurred_at="2026-09-10T12:01:00+00:00",
        run_id=run,
        event_id=new_event_id(),
    )
    assert a.run_id == b.run_id == run
    assert a.event_id != b.event_id
    assert legacy_run_id("abc-def-ghi").startswith("legacy-")


def test_stale_event_not_current_health() -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    old = "2026-09-01T12:00:00+00:00"
    assert event_is_stale(old, now=now, max_age=timedelta(days=1)) is True
    fresh = "2026-09-10T11:30:00+00:00"
    assert event_is_stale(fresh, now=now, max_age=timedelta(hours=2)) is False


def test_occurred_vs_received_preserved() -> None:
    event = CanonicalEvent(
        schema_version=1,
        event_id=new_event_id(),
        event_type=EventType.AUDIT_COMPLETED.value,
        run_id=new_run_id(),
        occurred_at="2026-09-10T10:00:00+00:00",
        received_at="2026-09-10T10:05:00+00:00",
        status="PASS",
        payload={"summary": {"result": "PASS"}},
        agent_id="agent-abcdefgh",
    )
    parsed = parse_event(event.to_dict())
    assert parsed.occurred_at != parsed.received_at
    assert parsed.occurred_at.startswith("2026-09-10T10:00")
