"""Local BackupLint scheduler (v0.4).

Runs coverage / integrity / restore checks on a schedule without changing
backup-truth semantics. Scheduling status is separate from audit PASS/FAIL.
"""

from __future__ import annotations

import json
import os
import random
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backuplint.audit import run_audit
from backuplint.compose import ComposeError
from backuplint.config import BackupLintConfig, ConfigError, load_config
from backuplint.restic import ResticError
from backuplint.schedule_config import (
    DEFAULT_INTERVALS,
    ScheduleCheckType,
    ScheduleConfig,
    ScheduleJobConfig,
)
from backuplint.schedule_lock import ScheduleLock, ScheduleLockError
from backuplint.schedule_store import ScheduleStore
from backuplint.timeutil import format_age

# Stable serialization order when multiple checks are due after downtime.
# Lighter checks run first; at most one check executes per tick.
_CHECK_RUN_ORDER: dict[ScheduleCheckType, int] = {
    ScheduleCheckType.COVERAGE: 0,
    ScheduleCheckType.INTEGRITY: 1,
    ScheduleCheckType.RESTORE_VERIFICATION: 2,
    ScheduleCheckType.DEEP_INTEGRITY: 3,
}

DEFER_REASON = "deferred: another scheduled check is running or queued"


class SchedulerError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class DueDecision:
    check_type: ScheduleCheckType
    due: bool
    next_run: datetime
    missed: bool = False


def default_state_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "backuplint" / "schedule"
    return Path.home() / ".local" / "share" / "backuplint" / "schedule"


def resolve_state_dir(schedule: ScheduleConfig) -> Path:
    if schedule.state_dir is not None:
        return schedule.state_dir
    return default_state_dir()


def compute_next_run(
    *,
    now: datetime,
    interval: timedelta,
    jitter: timedelta | None = None,
    rng: random.Random | None = None,
) -> datetime:
    """Return next run time after ``now`` with optional bounded jitter."""
    base = now + interval
    if jitter is None or jitter.total_seconds() <= 0:
        return base
    max_seconds = int(jitter.total_seconds())
    if max_seconds <= 0:
        return base
    generator = rng if rng is not None else random.SystemRandom()
    # Jitter is additive 0..jitter so runs never fire earlier than interval.
    return base + timedelta(seconds=generator.randint(0, max_seconds))


def decide_due(
    *,
    job: ScheduleJobConfig,
    stored_next: datetime | None,
    now: datetime,
    jitter: timedelta | None = None,
    rng: random.Random | None = None,
) -> DueDecision:
    """Missed-run policy: if next_run is in the past, run once (catch-up), not N times.

    Clock notes:
    - ``now`` and ``stored_next`` are compared in absolute UTC instants.
    - A forward jump makes overdue jobs due once (collapsed catch-up).
    - A backward jump that leaves ``stored_next`` still in the future waits;
      it does not invent extra runs.
    - Jitter is applied only when computing the *next* run after a due fire,
      and never schedules earlier than ``now + interval``.
    """
    now = _ensure_utc(now)
    if stored_next is not None:
        stored_next = _ensure_utc(stored_next)
    if stored_next is None:
        next_run = compute_next_run(
            now=now, interval=job.every, jitter=jitter, rng=rng
        )
        # First start: run immediately so cold start verifies quickly.
        return DueDecision(
            check_type=job.check_type,
            due=True,
            next_run=next_run,
            missed=False,
        )
    if stored_next <= now:
        next_run = compute_next_run(
            now=now, interval=job.every, jitter=jitter, rng=rng
        )
        return DueDecision(
            check_type=job.check_type,
            due=True,
            next_run=next_run,
            missed=True,
        )
    return DueDecision(
        check_type=job.check_type,
        due=False,
        next_run=stored_next,
        missed=False,
    )


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def audit_overrides_for(check_type: ScheduleCheckType) -> tuple[str, str]:
    """Return (integrity_cli, restore_verify_cli) overrides for a scheduled check."""
    if check_type is ScheduleCheckType.COVERAGE:
        return "off", "off"
    if check_type is ScheduleCheckType.INTEGRITY:
        return "standard", "off"
    if check_type is ScheduleCheckType.DEEP_INTEGRITY:
        return "deep", "off"
    if check_type is ScheduleCheckType.RESTORE_VERIFICATION:
        return "off", "selected"
    raise SchedulerError(f"Unknown check type: {check_type}")


def result_label(exit_code: int) -> str:
    if exit_code == 0:
        return "PASS"
    if exit_code == 1:
        return "FAIL"
    return "ERROR"


def run_scheduled_check(
    *,
    compose_file: Path,
    config_path: Path,
    check_type: ScheduleCheckType,
) -> tuple[str, int, str | None, object | None]:
    """Execute one scheduled audit.

    Returns (result, exit_code, detail, audit_outcome_or_none).
    """
    integrity_cli, restore_cli = audit_overrides_for(check_type)
    try:
        outcome = run_audit(
            compose_file,
            config_path=config_path,
            integrity_cli=integrity_cli,
            restore_verify_cli=restore_cli,
        )
    except (ComposeError, ConfigError, ResticError) as exc:
        return "ERROR", 2, exc.message, None
    except Exception as exc:  # noqa: BLE001 - boundary for scheduler durability
        return "ERROR", 2, f"unexpected scheduler error: {exc}", None
    return (
        result_label(outcome.summary.exit_code),
        outcome.summary.exit_code,
        None,
        outcome,
    )

@dataclass
class Scheduler:
    """Long-running local scheduler."""

    compose_file: Path
    config_path: Path
    backup_config: BackupLintConfig
    schedule: ScheduleConfig
    store: ScheduleStore
    lock: ScheduleLock
    poll_seconds: float = 5.0
    _stop: bool = False
    _running_check: ScheduleCheckType | None = None
    _rng: random.Random | None = None
    _clock: Callable[[], datetime] | None = None

    @classmethod
    def from_paths(
        cls,
        *,
        compose_file: Path,
        config_path: Path,
        poll_seconds: float = 5.0,
        rng: random.Random | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> Scheduler:
        backup_config = load_config(config_path)
        if backup_config.schedule is None:
            raise SchedulerError(
                "Configuration has no 'schedule' section; cannot start daemon."
            )
        state_dir = resolve_state_dir(backup_config.schedule)
        store = ScheduleStore(
            state_dir / "history.sqlite3",
            history_limit=backup_config.schedule.history_limit,
        )
        lock = ScheduleLock(state_dir / "daemon.lock")
        return cls(
            compose_file=compose_file,
            config_path=config_path,
            backup_config=backup_config,
            schedule=backup_config.schedule,
            store=store,
            lock=lock,
            poll_seconds=poll_seconds,
            _rng=rng,
            _clock=clock,
        )

    def _now(self) -> datetime:
        if self._clock is not None:
            return _ensure_utc(self._clock())
        return datetime.now(UTC)

    def request_stop(self, *_args: object) -> None:
        self._stop = True

    def run_forever(self) -> None:
        try:
            self.lock.acquire(blocking=False)
        except ScheduleLockError as exc:
            raise SchedulerError(exc.message) from exc
        previous_term = signal.signal(signal.SIGTERM, self.request_stop)
        previous_int = signal.signal(signal.SIGINT, self.request_stop)
        try:
            self._recover_after_restart()
            while not self._stop:
                self._tick()
                # Sleep in small slices so shutdown is responsive.
                deadline = time.monotonic() + self.poll_seconds
                while not self._stop and time.monotonic() < deadline:
                    time.sleep(min(0.25, deadline - time.monotonic()))
        finally:
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)
            # Never leave an in-memory running mark after shutdown.
            self._running_check = None
            self.lock.release()
            self.store.close()

    def _recover_after_restart(self) -> None:
        """Clear ephemeral run state; overdue next_run values stay due for catch-up.

        ``next_run`` is advanced only after a check finishes (``record_run``), so a
        crash mid-check does not permanently block future runs and does not require
        ScheduleStore schema changes.
        """
        self._running_check = None
        self._stop = False

    def _tick(self) -> None:
        # Re-entrancy / overlap guard: never start a second check while one runs.
        if self._running_check is not None:
            for job in self.schedule.enabled_jobs:
                state = self.store.get_job_state(job.check_type)
                stored_next = state.next_run if state is not None else None
                if stored_next is None or stored_next <= self._now():
                    if job.check_type is not self._running_check:
                        self.store.set_skipped(job.check_type, DEFER_REASON)
            return

        now = self._now()
        due_jobs: list[tuple[ScheduleJobConfig, DueDecision]] = []
        for job in self.schedule.enabled_jobs:
            state = self.store.get_job_state(job.check_type)
            stored_next = state.next_run if state is not None else None
            decision = decide_due(
                job=job,
                stored_next=stored_next,
                now=now,
                jitter=self.schedule.jitter,
                rng=self._rng,
            )
            if decision.due:
                due_jobs.append((job, decision))

        if not due_jobs:
            return

        # Serialize repository-level work: one check at a time.
        # Prefer lighter checks first when multiple are due after an outage.
        due_jobs.sort(key=lambda item: _CHECK_RUN_ORDER[item[0].check_type])

        # Run only the first due job this tick; others remain due for later ticks.
        # Missed windows collapse to a single catch-up (decide_due already).
        job, decision = due_jobs[0]
        for skipped_job, _skipped in due_jobs[1:]:
            self.store.set_skipped(skipped_job.check_type, DEFER_REASON)
        self._execute(job, decision)

    def _execute(self, job: ScheduleJobConfig, decision: DueDecision) -> None:
        self._running_check = job.check_type
        started = self._now()
        outcome: object | None = None
        try:
            result, exit_code, detail, outcome = run_scheduled_check(
                compose_file=self.compose_file,
                config_path=self.config_path,
                check_type=job.check_type,
            )
            finished = self._now()
            duration = max(0.0, (finished - started).total_seconds())
            self.store.record_run(
                check_type=job.check_type,
                started_at=started,
                finished_at=finished,
                result=result,
                exit_code=exit_code,
                duration_seconds=duration,
                detail=detail,
                next_run=decision.next_run,
            )
            if outcome is not None:
                self._maybe_submit_fleet(outcome, scanned_at=finished)
                self._maybe_submit_local_siem(outcome, scanned_at=finished)
            elif result == "ERROR":
                self._maybe_submit_fleet_error(detail, scanned_at=finished)
                self._maybe_submit_local_siem_error(detail, scanned_at=finished)
        finally:
            # Success, FAIL/ERROR, exception, or shutdown mid-flight: clear mark.
            self._running_check = None

    def _maybe_submit_fleet(self, outcome: object, *, scanned_at: datetime) -> None:
        """Best-effort submit of local result; never changes backup truth."""
        from backuplint.reporting import format_audit_json

        findings = getattr(outcome, "findings", [])
        integrity = getattr(outcome, "integrity", None)
        restore = getattr(outcome, "restore", None)
        result_obj = json.loads(
            format_audit_json(findings, integrity=integrity, restore=restore)
        )
        self._submit_fleet_result(result_obj, scanned_at=scanned_at)

    def _maybe_submit_fleet_error(
        self, detail: str | None, *, scanned_at: datetime
    ) -> None:
        from backuplint.engine import classify_engine_exception
        from backuplint.reporting import format_operational_error_json

        message = (detail or "scheduled check failed").strip()
        eng_err = classify_engine_exception(ResticError(message))
        result_obj = json.loads(
            format_operational_error_json(
                message=eng_err.message,
                kind=eng_err.kind.value,
                engine=eng_err.engine_id,
            )
        )
        self._submit_fleet_result(result_obj, scanned_at=scanned_at)

    def _submit_fleet_result(
        self, result_obj: dict[str, object], *, scanned_at: datetime
    ) -> None:
        fleet = self.backup_config.fleet
        if fleet is None:
            return
        try:
            import platform as plat

            from backuplint import __version__
            from backuplint.events import new_run_id
            from backuplint.fleet.agent import AgentIdentity, AgentQueue, FleetAgent
            from backuplint.fleet.protocol import (
                PROTOCOL_VERSION,
                ResultEnvelope,
                new_submission_id,
            )

            identity = AgentIdentity.load(fleet.identity_dir)
            agent = FleetAgent(
                controller_url=fleet.controller_url,
                identity=identity,
                queue=AgentQueue(fleet.identity_dir / "queue.jsonl"),
            )
            envelope = ResultEnvelope(
                agent_id=identity.agent_id,
                submission_id=new_submission_id(),
                scan_time=scanned_at.isoformat(),
                backuplint_version=__version__,
                platform=f"{plat.system()} {plat.machine()} {plat.release()}",
                result=result_obj,
                protocol_version=PROTOCOL_VERSION,
                run_id=new_run_id(),
            )
            try:
                agent.submit_envelope(envelope)
            except Exception:  # noqa: BLE001 - keep queued for later flush
                agent.queue.enqueue(envelope)
        except Exception:  # noqa: BLE001 - fleet failures must not crash daemon
            return

    def _maybe_submit_local_siem(self, outcome: object, *, scanned_at: datetime) -> None:
        from backuplint.siem.runtime import LocalSiemFeed, should_use_local_siem_feed

        if not should_use_local_siem_feed(
            self.backup_config.siem,
            has_fleet=self.backup_config.fleet is not None,
        ):
            return
        state_dir = resolve_state_dir(self.schedule)
        feed = LocalSiemFeed.open(state_dir, self.backup_config.siem)
        try:
            feed.submit_outcome(outcome, occurred_at=scanned_at.isoformat())
            feed.flush()
        except Exception:  # noqa: BLE001
            return
        finally:
            feed.close()

    def _maybe_submit_local_siem_error(
        self, detail: str | None, *, scanned_at: datetime
    ) -> None:
        from backuplint.engine import classify_engine_exception
        from backuplint.reporting import format_operational_error_json
        from backuplint.siem.event import audit_result_to_siem_event
        from backuplint.siem.runtime import LocalSiemFeed, should_use_local_siem_feed

        if not should_use_local_siem_feed(
            self.backup_config.siem,
            has_fleet=self.backup_config.fleet is not None,
        ):
            return
        message = (detail or "scheduled check failed").strip()
        eng_err = classify_engine_exception(ResticError(message))
        result_obj = json.loads(
            format_operational_error_json(
                message=eng_err.message,
                kind=eng_err.kind.value,
                engine=eng_err.engine_id,
            )
        )
        state_dir = resolve_state_dir(self.schedule)
        feed = LocalSiemFeed.open(state_dir, self.backup_config.siem)
        try:
            if feed.exporter is not None:
                when = scanned_at.isoformat()
                event = audit_result_to_siem_event(
                    result_obj,
                    occurred_at=when,
                    source_role="standalone",
                    received_at=when,
                )
                if event is not None:
                    feed.exporter.submit(event)
            feed.flush()
        except Exception:  # noqa: BLE001
            return
        finally:
            feed.close()


def format_schedule_status(
    *,
    schedule: ScheduleConfig,
    store: ScheduleStore,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(UTC)
    lines = ["BackupLint Schedule", ""]
    width = max(len(j.check_type.value) for j in schedule.jobs) if schedule.jobs else 10
    for job in schedule.jobs:
        if not job.enabled:
            lines.append(f"{job.check_type.value:<{width}}  disabled")
            continue
        state = store.get_job_state(job.check_type)
        if state is None or state.last_result is None:
            result = "—"
            ago = "never"
        else:
            result = state.last_result
            if state.last_finished is not None:
                ago = f"{format_age(now - state.last_finished)} ago"
            else:
                ago = "unknown"
        if state is not None and state.next_run is not None:
            if state.next_run <= now:
                nxt = "due"
            else:
                nxt = f"next {format_age(state.next_run - now)}"
        else:
            nxt = f"every {format_age(job.every)}"
        stale = ""
        if (
            state is not None
            and state.last_finished is not None
            and state.next_run is not None
            and state.next_run < now - timedelta(seconds=1)
            and state.last_result == "PASS"
        ):
            # Do not imply PASS is current merely because last result was PASS.
            stale = "  (stale schedule)"
        lines.append(
            f"{job.check_type.value:<{width}}  {result:<5}  {ago:<12}  {nxt}{stale}"
        )
        if state is not None and state.skipped_reason:
            lines.append(f"{'':<{width}}  note: {state.skipped_reason}")
    lines.append("")
    lines.append(
        "Note: schedule status is not a substitute for backup verification truth."
    )
    return "\n".join(lines)


def schedule_status_payload(
    *,
    schedule: ScheduleConfig,
    store: ScheduleStore,
    now: datetime | None = None,
) -> dict[str, object]:
    now = now or datetime.now(UTC)
    jobs: list[dict[str, object]] = []
    for job in schedule.jobs:
        state = store.get_job_state(job.check_type)
        jobs.append(
            {
                "check_type": job.check_type.value,
                "enabled": job.enabled,
                "every_seconds": job.every.total_seconds(),
                "last_result": state.last_result if state else None,
                "last_finished": (
                    state.last_finished.isoformat()
                    if state and state.last_finished
                    else None
                ),
                "last_success": (
                    state.last_success.isoformat()
                    if state and state.last_success
                    else None
                ),
                "last_failure": (
                    state.last_failure.isoformat()
                    if state and state.last_failure
                    else None
                ),
                "next_run": (
                    state.next_run.isoformat() if state and state.next_run else None
                ),
                "skipped_reason": state.skipped_reason if state else None,
                "due": bool(
                    state and state.next_run is not None and state.next_run <= now
                ),
            }
        )
    return {
        "schedule": {
            "now": now.isoformat(),
            "jobs": jobs,
            "history_limit": schedule.history_limit,
            "defaults": {
                key.value: int(value.total_seconds())
                for key, value in DEFAULT_INTERVALS.items()
            },
        }
    }
