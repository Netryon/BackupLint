"""Rollout state machine tests."""

from __future__ import annotations

import pytest

from backuplint.policy.rollout import (
    RolloutError,
    RolloutStatus,
    transition_rollout,
    validate_rollout_config,
)


def test_rollout_transitions() -> None:
    assert transition_rollout(RolloutStatus.PENDING, RolloutStatus.RUNNING) == RolloutStatus.RUNNING
    assert transition_rollout(RolloutStatus.RUNNING, RolloutStatus.PAUSED) == RolloutStatus.PAUSED


def test_invalid_transition() -> None:
    with pytest.raises(RolloutError):
        transition_rollout(RolloutStatus.COMPLETED, RolloutStatus.RUNNING)


def test_validate_config_bounds() -> None:
    validate_rollout_config(
        batch_size=10,
        max_concurrent=5,
        pause_between_batches_seconds=30,
        failure_threshold=3,
    )
    with pytest.raises(RolloutError):
        validate_rollout_config(
            batch_size=0,
            max_concurrent=5,
            pause_between_batches_seconds=30,
            failure_threshold=3,
        )
