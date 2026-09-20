"""Schedule configuration types and parsing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path

from backuplint.errors import ConfigError
from backuplint.timeutil import parse_duration


class ScheduleCheckType(StrEnum):
    COVERAGE = "coverage"
    INTEGRITY = "integrity"
    DEEP_INTEGRITY = "deep_integrity"
    RESTORE_VERIFICATION = "restore_verification"


# Conservative production defaults (documented).
DEFAULT_INTERVALS: dict[ScheduleCheckType, timedelta] = {
    ScheduleCheckType.COVERAGE: timedelta(minutes=30),
    ScheduleCheckType.INTEGRITY: timedelta(hours=6),
    ScheduleCheckType.RESTORE_VERIFICATION: timedelta(hours=24),
    ScheduleCheckType.DEEP_INTEGRITY: timedelta(days=7),
}


@dataclass(frozen=True)
class ScheduleJobConfig:
    """One scheduled check kind."""

    check_type: ScheduleCheckType
    every: timedelta
    enabled: bool = True


@dataclass(frozen=True)
class ScheduleConfig:
    """Local automation schedule (v0.4). Absent means scheduling is not configured."""

    jobs: tuple[ScheduleJobConfig, ...] = ()
    state_dir: Path | None = None
    jitter: timedelta | None = None
    history_limit: int = 500

    def job_for(self, check_type: ScheduleCheckType) -> ScheduleJobConfig | None:
        for job in self.jobs:
            if job.check_type is check_type:
                return job
        return None

    @property
    def enabled_jobs(self) -> tuple[ScheduleJobConfig, ...]:
        return tuple(job for job in self.jobs if job.enabled)


def _parse_duration_field(raw: object, *, field_name: str) -> timedelta:
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"'{field_name}' must be a non-empty duration string.")
    try:
        value = parse_duration(raw)
    except ValueError as exc:
        raise ConfigError(f"Invalid {field_name}: {exc}") from exc
    if value.total_seconds() <= 0:
        raise ConfigError(f"'{field_name}' must be greater than zero.")
    return value


def _parse_job(
    check_type: ScheduleCheckType,
    raw: object,
    *,
    default_every: timedelta,
) -> ScheduleJobConfig | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        if raw is False:
            return ScheduleJobConfig(
                check_type=check_type, every=default_every, enabled=False
            )
        raise ConfigError(
            f"'schedule.{check_type.value}' must be a mapping or false "
            "(quote durations; do not use bare true)."
        )
    if not isinstance(raw, dict):
        raise ConfigError(f"'schedule.{check_type.value}' must be a mapping.")

    allowed = {"every", "enabled"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(
            f"Unknown schedule.{check_type.value} key(s): "
            + ", ".join(repr(key) for key in unknown)
        )

    every = default_every
    if "every" in raw:
        every = _parse_duration_field(
            raw["every"],
            field_name=f"schedule.{check_type.value}.every",
        )

    enabled = True
    if "enabled" in raw:
        enabled_raw = raw["enabled"]
        if not isinstance(enabled_raw, bool):
            raise ConfigError(
                f"'schedule.{check_type.value}.enabled' must be a boolean."
            )
        enabled = enabled_raw

    return ScheduleJobConfig(check_type=check_type, every=every, enabled=enabled)


def parse_schedule(raw: object, *, config_dir: Path | None) -> ScheduleConfig | None:
    """Parse optional top-level ``schedule`` mapping. None means not configured."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'schedule' must be a mapping.")

    allowed = {
        "coverage",
        "integrity",
        "deep_integrity",
        "restore_verification",
        "state_dir",
        "jitter",
        "history_limit",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(
            "Unknown schedule key(s): " + ", ".join(repr(key) for key in unknown)
        )

    jobs: list[ScheduleJobConfig] = []
    for check_type, default in DEFAULT_INTERVALS.items():
        job = _parse_job(check_type, raw.get(check_type.value), default_every=default)
        if job is not None:
            jobs.append(job)

    state_dir: Path | None = None
    state_dir_raw = raw.get("state_dir")
    if state_dir_raw is not None:
        if not isinstance(state_dir_raw, str) or not state_dir_raw.strip():
            raise ConfigError("'schedule.state_dir' must be a non-empty string path.")
        candidate = Path(state_dir_raw.strip()).expanduser()
        if not candidate.is_absolute() and config_dir is not None:
            state_dir = (config_dir / candidate).resolve()
        else:
            state_dir = candidate.expanduser()

    jitter: timedelta | None = None
    if "jitter" in raw and raw["jitter"] is not None:
        jitter = _parse_duration_field(raw["jitter"], field_name="schedule.jitter")

    history_limit = 500
    if "history_limit" in raw and raw["history_limit"] is not None:
        limit_raw = raw["history_limit"]
        if isinstance(limit_raw, bool) or not isinstance(limit_raw, int):
            raise ConfigError("'schedule.history_limit' must be a positive integer.")
        if limit_raw < 1:
            raise ConfigError("'schedule.history_limit' must be a positive integer.")
        history_limit = limit_raw

    if not jobs:
        raise ConfigError(
            "'schedule' must define at least one check "
            "(coverage, integrity, deep_integrity, or restore_verification)."
        )

    return ScheduleConfig(
        jobs=tuple(jobs),
        state_dir=state_dir,
        jitter=jitter,
        history_limit=history_limit,
    )
