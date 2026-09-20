"""Bounded staged rollout state machine (v0.8)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RolloutStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABORTED = "aborted"
    FAILED = "failed"


class RolloutMemberStatus(StrEnum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    APPLIED = "applied"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class RolloutConfig:
    rollout_id: str
    policy_id: str
    revision_id: str
    target_group_id: str | None
    batch_size: int
    max_concurrent: int
    pause_between_batches_seconds: int
    failure_threshold: int
    status: RolloutStatus
    created_at: str
    started_at: str | None
    completed_at: str | None
    paused_at: str | None
    created_by: str


_ALLOWED_TRANSITIONS: dict[RolloutStatus, frozenset[RolloutStatus]] = {
    RolloutStatus.PENDING: frozenset({RolloutStatus.RUNNING, RolloutStatus.ABORTED}),
    RolloutStatus.RUNNING: frozenset(
        {RolloutStatus.PAUSED, RolloutStatus.COMPLETED, RolloutStatus.FAILED, RolloutStatus.ABORTED}
    ),
    RolloutStatus.PAUSED: frozenset({RolloutStatus.RUNNING, RolloutStatus.ABORTED}),
    RolloutStatus.COMPLETED: frozenset(),
    RolloutStatus.ABORTED: frozenset(),
    RolloutStatus.FAILED: frozenset(),
}


class RolloutError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def validate_rollout_config(
    *,
    batch_size: int,
    max_concurrent: int,
    pause_between_batches_seconds: int,
    failure_threshold: int,
) -> None:
    if batch_size < 1 or batch_size > 500:
        raise RolloutError("batch_size must be 1..500")
    if max_concurrent < 1 or max_concurrent > 500:
        raise RolloutError("max_concurrent must be 1..500")
    if pause_between_batches_seconds < 0 or pause_between_batches_seconds > 3600:
        raise RolloutError("pause_between_batches_seconds must be 0..3600")
    if failure_threshold < 1 or failure_threshold > 100:
        raise RolloutError("failure_threshold must be 1..100")


def transition_rollout(current: RolloutStatus, target: RolloutStatus) -> RolloutStatus:
    allowed = _ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise RolloutError(f"cannot transition rollout from {current} to {target}")
    return target


def compute_batch_number(member_index: int, batch_size: int) -> int:
    return member_index // max(1, batch_size)
