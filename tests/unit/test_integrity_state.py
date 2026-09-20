"""Unit tests for integrity state persistence."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backuplint.config import IntegrityMode
from backuplint.integrity_state import (
    IntegrityStateError,
    IntegrityStateRecord,
    is_integrity_success_stale,
    last_success_for_repository,
    load_integrity_state,
    record_integrity_success,
    repository_state_key,
)


def test_repository_key_redacts_userinfo() -> None:
    plain = repository_state_key("sftp://backup.example/repo")
    with_user = repository_state_key("sftp://user:secret@backup.example/repo")
    assert plain == with_user
    assert "secret" not in with_user


def test_record_and_load_success(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    when = datetime(2026, 9, 8, 20, 0, tzinfo=UTC)
    record_integrity_success(
        state,
        repository="/var/backups/restic",
        mode=IntegrityMode.STANDARD,
        checked_at=when,
        restic_version="0.18.1",
    )
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    body = json.dumps(payload)
    assert "password" not in body
    assert "/var/backups/restic" not in body
    records = load_integrity_state(state)
    record = last_success_for_repository(records, "/var/backups/restic")
    assert record is not None
    assert record.mode is IntegrityMode.STANDARD
    assert record.restic_version == "0.18.1"
    assert record.last_success == when


def test_failure_does_not_clear_prior_success(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    record_integrity_success(
        state,
        repository="/repo",
        mode=IntegrityMode.STANDARD,
        checked_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    # Simulate a later failure by not calling record_integrity_success.
    records = load_integrity_state(state)
    record = last_success_for_repository(records, "/repo")
    assert record is not None
    assert record.last_success.year == 2026


def test_stale_boundaries() -> None:
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    fresh = IntegrityStateRecord(
        last_success=now - timedelta(hours=23),
        mode=IntegrityMode.STANDARD,
    )
    stale = IntegrityStateRecord(
        last_success=now - timedelta(hours=25),
        mode=IntegrityMode.STANDARD,
    )
    assert is_integrity_success_stale(fresh, max_age=timedelta(hours=24), now=now) is False
    assert is_integrity_success_stale(stale, max_age=timedelta(hours=24), now=now) is True
    assert is_integrity_success_stale(None, max_age=timedelta(hours=24), now=now) is True


def test_future_timestamp_is_stale() -> None:
    now = datetime(2026, 9, 8, tzinfo=UTC)
    future = IntegrityStateRecord(
        last_success=now + timedelta(days=1),
        mode=IntegrityMode.DEEP,
    )
    assert is_integrity_success_stale(future, max_age=timedelta(days=7), now=now) is True


def test_malformed_state_errors(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(IntegrityStateError):
        load_integrity_state(path)


def test_atomic_replace_keeps_permissions(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    record_integrity_success(
        state,
        repository="/repo",
        mode=IntegrityMode.DEEP,
        checked_at=datetime.now(UTC),
    )
    assert oct(state.stat().st_mode & 0o777) == "0o600"
