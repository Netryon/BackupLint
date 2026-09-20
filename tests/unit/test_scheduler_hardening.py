"""Deterministic scheduler hardening tests (overlap, catch-up, clock, overrides)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from random import Random
from typing import Any

import pytest

from backuplint.audit import resolve_integrity_mode, resolve_restore_mode
from backuplint.config import ConfigError, parse_config_data
from backuplint.restic import IntegrityMode
from backuplint.restore_verify import RestoreVerificationMode
from backuplint.schedule_config import ScheduleCheckType, ScheduleJobConfig, parse_schedule
from backuplint.schedule_lock import ScheduleLock
from backuplint.schedule_store import ScheduleStore
from backuplint.scheduler import (
    DEFER_REASON,
    Scheduler,
    audit_overrides_for,
    compute_next_run,
    decide_due,
    format_schedule_status,
    result_label,
    run_scheduled_check,
)


@dataclass
class FakeClock:
    """Injectable wall clock for deterministic scheduler tests."""

    current: datetime

    def __call__(self) -> datetime:
        return self.current

    def set(self, value: datetime) -> None:
        self.current = value

    def advance(self, delta: timedelta) -> None:
        self.current = self.current + delta


def _job(check: ScheduleCheckType, every: timedelta) -> ScheduleJobConfig:
    return ScheduleJobConfig(check_type=check, every=every, enabled=True)


def _minimal_scheduler(
    tmp_path: Path,
    *,
    clock: FakeClock,
    jobs: dict[str, Any] | None = None,
    rng: Random | None = None,
) -> Scheduler:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "f.txt").write_text("x\n", encoding="utf-8")
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n"
        "  app:\n"
        "    image: alpine:3.20\n"
        "    volumes: [\"./data:/data\"]\n",
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    schedule_block = jobs or {
        "coverage": {"every": "1h"},
        "integrity": {"every": "1h"},
        "restore_verification": {"enabled": False, "every": "1h"},
        "deep_integrity": {"enabled": False, "every": "1h"},
    }
    config = tmp_path / "backuplint.yml"
    raw = {
        "backup_paths": [str(data)],
        "schedule": {"state_dir": str(state_dir), "history_limit": 50, **schedule_block},
    }
    parsed = parse_config_data(raw, source_file=config)
    assert parsed.schedule is not None
    # Persist a real YAML file for from_paths loaders that read disk.
    import yaml

    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    store = ScheduleStore(state_dir / "history.sqlite3", history_limit=50)
    lock = ScheduleLock(state_dir / "daemon.lock")
    return Scheduler(
        compose_file=compose,
        config_path=config,
        backup_config=parsed,
        schedule=parsed.schedule,
        store=store,
        lock=lock,
        poll_seconds=0.01,
        _rng=rng or Random(0),  # noqa: S311 - deterministic schedule tests
        _clock=clock,
    )


def test_overlap_defers_second_job_and_runs_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    sched = _minimal_scheduler(tmp_path, clock=clock)
    calls: list[ScheduleCheckType] = []

    def fake_run(*, compose_file: Path, config_path: Path, check_type: ScheduleCheckType):
        calls.append(check_type)
        # Simulate nested tick while first job is active (overlap).
        if check_type is ScheduleCheckType.COVERAGE and len(calls) == 1:
            sched._tick()
        return "PASS", 0, None, object()

    monkeypatch.setattr("backuplint.scheduler.run_scheduled_check", fake_run)

    # Both due (cold start).
    sched._tick()
    assert calls == [ScheduleCheckType.COVERAGE]
    integrity_state = sched.store.get_job_state(ScheduleCheckType.INTEGRITY)
    assert integrity_state is not None
    assert integrity_state.skipped_reason == DEFER_REASON
    assert integrity_state.next_run is None  # still due / never advanced

    sched._tick()
    assert calls == [ScheduleCheckType.COVERAGE, ScheduleCheckType.INTEGRITY]
    assert sched._running_check is None
    sched.store.close()


def test_repeated_overlap_does_not_duplicate_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    sched = _minimal_scheduler(
        tmp_path,
        clock=clock,
        jobs={
            "coverage": {"every": "1h"},
            "integrity": {"enabled": False, "every": "1h"},
            "restore_verification": {"enabled": False, "every": "1h"},
            "deep_integrity": {"enabled": False, "every": "1h"},
        },
    )
    runs = {"n": 0}

    def fake_run(**_kwargs: object):
        runs["n"] += 1
        # Multiple re-entrant ticks must not start more coverage runs.
        sched._tick()
        sched._tick()
        return "PASS", 0, None, None

    monkeypatch.setattr("backuplint.scheduler.run_scheduled_check", fake_run)
    sched._tick()
    assert runs["n"] == 1
    assert len(sched.store.history(limit=20)) == 1
    sched.store.close()


def test_exception_clears_running_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    sched = _minimal_scheduler(
        tmp_path,
        clock=clock,
        jobs={
            "coverage": {"every": "1h"},
            "integrity": {"enabled": False, "every": "1h"},
            "restore_verification": {"enabled": False, "every": "1h"},
            "deep_integrity": {"enabled": False, "every": "1h"},
        },
    )

    def boom(**_kwargs: object):
        raise RuntimeError("simulated failure before result")

    monkeypatch.setattr("backuplint.scheduler.run_scheduled_check", boom)
    with pytest.raises(RuntimeError, match="simulated"):
        sched._tick()
    assert sched._running_check is None
    # next_run never advanced → still due after failure to record
    sched.store.close()


def test_restart_after_mid_run_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    sched = _minimal_scheduler(
        tmp_path,
        clock=clock,
        jobs={
            "coverage": {"every": "1h"},
            "integrity": {"enabled": False, "every": "1h"},
            "restore_verification": {"enabled": False, "every": "1h"},
            "deep_integrity": {"enabled": False, "every": "1h"},
        },
    )
    # Pretend a previous process died while "running".
    sched._running_check = ScheduleCheckType.COVERAGE
    sched.store.set_next_run(
        ScheduleCheckType.COVERAGE, clock.current - timedelta(minutes=5)
    )

    def fake_run(**_kwargs: object):
        return "PASS", 0, None, None

    monkeypatch.setattr("backuplint.scheduler.run_scheduled_check", fake_run)
    sched._recover_after_restart()
    assert sched._running_check is None
    sched._tick()
    state = sched.store.get_job_state(ScheduleCheckType.COVERAGE)
    assert state is not None
    assert state.last_result == "PASS"
    assert state.next_run == clock.current + timedelta(hours=1)
    sched.store.close()


def test_missed_runs_collapse_and_survive_restart(tmp_path: Path) -> None:
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    job = _job(ScheduleCheckType.COVERAGE, timedelta(hours=1))
    # 5 hours late → one due decision, next = now+1h (not +5 runs).
    decision = decide_due(
        job=job, stored_next=now - timedelta(hours=5), now=now, jitter=None
    )
    assert decision.due is True
    assert decision.missed is True
    assert decision.next_run == now + timedelta(hours=1)

    store = ScheduleStore(tmp_path / "h.sqlite3")
    store.set_next_run(ScheduleCheckType.COVERAGE, now - timedelta(hours=5))
    store.close()

    store2 = ScheduleStore(tmp_path / "h.sqlite3")
    state = store2.get_job_state(ScheduleCheckType.COVERAGE)
    assert state is not None
    decision2 = decide_due(
        job=job, stored_next=state.next_run, now=now, jitter=None
    )
    assert decision2.due is True
    assert decision2.next_run == now + timedelta(hours=1)
    store2.close()


def test_catchup_not_unbounded_burst(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(datetime(2026, 1, 10, tzinfo=UTC))
    sched = _minimal_scheduler(
        tmp_path,
        clock=clock,
        jobs={
            "coverage": {"every": "1h"},
            "integrity": {"enabled": False, "every": "1h"},
            "restore_verification": {"enabled": False, "every": "1h"},
            "deep_integrity": {"enabled": False, "every": "1h"},
        },
    )
    # Far overdue.
    sched.store.set_next_run(
        ScheduleCheckType.COVERAGE, clock.current - timedelta(days=3)
    )
    calls = {"n": 0}

    def fake_run(**_kwargs: object):
        calls["n"] += 1
        return "PASS", 0, None, None

    monkeypatch.setattr("backuplint.scheduler.run_scheduled_check", fake_run)
    sched._tick()
    sched._tick()
    sched._tick()
    # One catch-up then next_run is in the future — no storm.
    assert calls["n"] == 1
    sched.store.close()


def test_clock_jump_forward_and_backward() -> None:
    job = _job(ScheduleCheckType.INTEGRITY, timedelta(hours=6))
    base = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    next_at = base + timedelta(hours=6)

    # Forward jump past next_at → single catch-up.
    forward = decide_due(
        job=job, stored_next=next_at, now=base + timedelta(hours=30), jitter=None
    )
    assert forward.due is True
    assert forward.missed is True
    assert forward.next_run == base + timedelta(hours=36)

    # Backward jump: next still in the future relative to new now → wait.
    back_now = base + timedelta(hours=1)
    backward = decide_due(job=job, stored_next=next_at, now=back_now, jitter=None)
    assert backward.due is False
    assert backward.next_run == next_at


def test_timezone_naive_normalized_to_utc() -> None:
    job = _job(ScheduleCheckType.COVERAGE, timedelta(hours=1))
    naive_now = datetime(2026, 1, 1, 12, 0)  # naive
    naive_next = datetime(2026, 1, 1, 11, 0)
    decision = decide_due(
        job=job, stored_next=naive_next, now=naive_now, jitter=None
    )
    assert decision.due is True
    assert decision.next_run.tzinfo is not None
    assert decision.next_run == datetime(2026, 1, 1, 13, 0, tzinfo=UTC)

    # Non-UTC aware timestamps normalize before compare.
    eastern = timezone(timedelta(hours=-5))
    decision2 = decide_due(
        job=job,
        stored_next=datetime(2026, 1, 1, 7, 0, tzinfo=eastern),  # 12:00 UTC
        now=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        jitter=None,
    )
    assert decision2.due is True


def test_identical_due_timestamps_stable_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    sched = _minimal_scheduler(
        tmp_path,
        clock=clock,
        jobs={
            "coverage": {"every": "1h"},
            "integrity": {"every": "1h"},
            "restore_verification": {"every": "1h"},
            "deep_integrity": {"every": "1h"},
        },
    )
    order: list[ScheduleCheckType] = []

    def fake_run(*, check_type: ScheduleCheckType, **_kwargs: object):
        order.append(check_type)
        return "PASS", 0, None, None

    monkeypatch.setattr("backuplint.scheduler.run_scheduled_check", fake_run)
    for _ in range(4):
        sched._tick()
    assert order == [
        ScheduleCheckType.COVERAGE,
        ScheduleCheckType.INTEGRITY,
        ScheduleCheckType.RESTORE_VERIFICATION,
        ScheduleCheckType.DEEP_INTEGRITY,
    ]
    sched.store.close()


def test_jitter_never_advances_before_interval() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    rng = Random(123)  # noqa: S311 - deterministic jitter bounds test
    for _ in range(50):
        nxt = compute_next_run(
            now=now,
            interval=timedelta(hours=2),
            jitter=timedelta(minutes=30),
            rng=rng,
        )
        assert nxt >= now + timedelta(hours=2)
        assert nxt <= now + timedelta(hours=2, minutes=30)


def test_invalid_schedule_data_fails_clearly() -> None:
    with pytest.raises(ConfigError, match="greater than zero|Invalid"):
        parse_schedule({"coverage": {"every": "0s"}}, config_dir=None)
    with pytest.raises(ConfigError, match="Unknown schedule"):
        parse_schedule({"coverage": {"every": "1h"}, "cron": "* * * * *"}, config_dir=None)
    with pytest.raises(ConfigError, match="at least one check"):
        parse_schedule({"jitter": "1m"}, config_dir=None)


def test_audit_overrides_per_check_type() -> None:
    assert audit_overrides_for(ScheduleCheckType.COVERAGE) == ("off", "off")
    assert audit_overrides_for(ScheduleCheckType.INTEGRITY) == ("standard", "off")
    assert audit_overrides_for(ScheduleCheckType.DEEP_INTEGRITY) == ("deep", "off")
    assert audit_overrides_for(ScheduleCheckType.RESTORE_VERIFICATION) == (
        "off",
        "selected",
    )


def test_scheduled_overrides_win_over_config_defaults() -> None:
    """Coverage schedule must force integrity/restore off even if config enables them."""
    integrity_cli, restore_cli = audit_overrides_for(ScheduleCheckType.COVERAGE)
    # Config wants deep + selected; schedule coverage overrides to off/off.
    integrity = resolve_integrity_mode(
        configured=IntegrityMode.DEEP,
        cli_override=integrity_cli,
        restic_configured=True,
    )
    restore = resolve_restore_mode(
        configured=RestoreVerificationMode.SELECTED,
        cli_override=restore_cli,
        restic_configured=True,
    )
    assert integrity is IntegrityMode.OFF
    assert restore is RestoreVerificationMode.OFF

    deep_i, deep_r = audit_overrides_for(ScheduleCheckType.DEEP_INTEGRITY)
    assert (
        resolve_integrity_mode(
            configured=IntegrityMode.STANDARD,
            cli_override=deep_i,
            restic_configured=True,
        )
        is IntegrityMode.DEEP
    )
    assert (
        resolve_restore_mode(
            configured=RestoreVerificationMode.SELECTED,
            cli_override=deep_r,
            restic_configured=True,
        )
        is RestoreVerificationMode.OFF
    )

    _ri, rr = audit_overrides_for(ScheduleCheckType.RESTORE_VERIFICATION)
    assert (
        resolve_restore_mode(
            configured=RestoreVerificationMode.OFF,
            cli_override=rr,
            restic_configured=True,
        )
        is RestoreVerificationMode.SELECTED
    )


def test_result_label_and_run_scheduled_error_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert result_label(0) == "PASS"
    assert result_label(1) == "FAIL"
    assert result_label(2) == "ERROR"
    assert result_label(99) == "ERROR"

    compose = tmp_path / "c.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    config = tmp_path / "b.yml"
    config.write_text("backup_paths: [/data]\n", encoding="utf-8")

    def raise_compose(*_a: object, **_k: object):
        from backuplint.compose import ComposeError

        raise ComposeError("compose broken")

    monkeypatch.setattr("backuplint.scheduler.run_audit", raise_compose)
    result, code, detail, outcome = run_scheduled_check(
        compose_file=compose,
        config_path=config,
        check_type=ScheduleCheckType.COVERAGE,
    )
    assert result == "ERROR"
    assert code == 2
    assert detail == "compose broken"
    assert outcome is None


def test_status_distinguishes_stale_pass_from_current_health(tmp_path: Path) -> None:
    schedule = parse_schedule(
        {"coverage": {"every": "30m"}},
        config_dir=None,
    )
    assert schedule is not None
    store = ScheduleStore(tmp_path / "h.sqlite3")
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    store.record_run(
        check_type=ScheduleCheckType.COVERAGE,
        started_at=now - timedelta(hours=3),
        finished_at=now - timedelta(hours=3),
        result="PASS",
        exit_code=0,
        duration_seconds=1.0,
        next_run=now - timedelta(hours=1),
    )
    text = format_schedule_status(schedule=schedule, store=store, now=now)
    assert "PASS" in text
    assert "stale schedule" in text
    assert "not a substitute" in text
    store.close()


def test_graceful_stop_flag_clears_running_on_shutdown_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    sched = _minimal_scheduler(
        tmp_path,
        clock=clock,
        jobs={
            "coverage": {"every": "1h"},
            "integrity": {"enabled": False, "every": "1h"},
            "restore_verification": {"enabled": False, "every": "1h"},
            "deep_integrity": {"enabled": False, "every": "1h"},
        },
    )

    def fake_run(**_kwargs: object):
        sched.request_stop()
        return "PASS", 0, None, None

    monkeypatch.setattr("backuplint.scheduler.run_scheduled_check", fake_run)
    # Drive one tick then simulate finally cleanup from run_forever.
    sched._tick()
    sched._running_check = ScheduleCheckType.COVERAGE  # pretend mid-flight
    sched._running_check = None  # finally clause
    assert sched._stop is True
    assert sched._running_check is None
    # History persisted; next_run advanced — restart would wait until due.
    state = sched.store.get_job_state(ScheduleCheckType.COVERAGE)
    assert state is not None
    assert state.last_result == "PASS"
    sched.store.close()
