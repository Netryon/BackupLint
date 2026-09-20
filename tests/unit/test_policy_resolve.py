"""Assignment resolution precedence tests."""

from __future__ import annotations

import pytest

from backuplint.policy.resolve import (
    AssignmentInputs,
    AssignmentSource,
    resolve_effective_assignment,
)
from backuplint.policy.schema import PolicyError


def test_explicit_over_group() -> None:
    resolved = resolve_effective_assignment(
        AssignmentInputs(
            agent_id="a1",
            explicit=("p1", "r1", "sha1"),
            group_assignments=(("g1", "p2", "r2", "sha2"),),
            default_assignment=("p3", "r3", "sha3"),
        )
    )
    assert resolved.source == AssignmentSource.AGENT
    assert resolved.revision_id == "r1"


def test_group_over_default() -> None:
    resolved = resolve_effective_assignment(
        AssignmentInputs(
            agent_id="a1",
            group_assignments=(("g1", "p2", "r2", "sha2"),),
            default_assignment=("p3", "r3", "sha3"),
        )
    )
    assert resolved.source == AssignmentSource.GROUP


def test_conflicting_groups_rejected() -> None:
    with pytest.raises(PolicyError, match="conflicting"):
        resolve_effective_assignment(
            AssignmentInputs(
                agent_id="a1",
                group_assignments=(
                    ("g1", "p1", "r1", "sha1"),
                    ("g2", "p2", "r2", "sha2"),
                ),
            )
        )
