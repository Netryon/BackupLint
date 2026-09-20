"""Effective policy assignment resolution with conflict detection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from backuplint.policy.errors import PolicyError


class AssignmentSource(StrEnum):
    AGENT = "agent"
    GROUP = "group"
    DEFAULT = "default"


@dataclass(frozen=True, slots=True)
class ResolvedAssignment:
    policy_id: str
    revision_id: str
    content_sha256: str
    source: AssignmentSource
    source_ref: str | None


@dataclass(frozen=True, slots=True)
class AssignmentInputs:
    """Inputs for resolving one agent's effective policy."""

    agent_id: str
    explicit: tuple[str, str, str] | None = None  # policy_id, revision_id, sha256
    group_assignments: tuple[tuple[str, str, str, str], ...] = ()
    # each: group_id, policy_id, revision_id, sha256
    default_assignment: tuple[str, str, str] | None = None  # policy_id, revision_id, sha256


def resolve_effective_assignment(inputs: AssignmentInputs) -> ResolvedAssignment:
    """Resolve effective policy: explicit > group > default.

    Conflicting group assignments raise PolicyError.
    """
    if inputs.explicit is not None:
        policy_id, revision_id, sha256 = inputs.explicit
        return ResolvedAssignment(
            policy_id=policy_id,
            revision_id=revision_id,
            content_sha256=sha256,
            source=AssignmentSource.AGENT,
            source_ref=inputs.agent_id,
        )
    if inputs.group_assignments:
        unique_policies: dict[str, tuple[str, str, str, str]] = {}
        for group_id, policy_id, revision_id, sha256 in inputs.group_assignments:
            key = f"{policy_id}:{revision_id}:{sha256}"
            unique_policies.setdefault(key, (group_id, policy_id, revision_id, sha256))
        if len(unique_policies) > 1:
            groups = sorted(g[0] for g in unique_policies.values())
            raise PolicyError(
                f"conflicting group policy assignments for agent {inputs.agent_id}: "
                f"groups={groups}"
            )
        group_id, policy_id, revision_id, sha256 = next(iter(unique_policies.values()))
        return ResolvedAssignment(
            policy_id=policy_id,
            revision_id=revision_id,
            content_sha256=sha256,
            source=AssignmentSource.GROUP,
            source_ref=group_id,
        )
    if inputs.default_assignment is not None:
        policy_id, revision_id, sha256 = inputs.default_assignment
        return ResolvedAssignment(
            policy_id=policy_id,
            revision_id=revision_id,
            content_sha256=sha256,
            source=AssignmentSource.DEFAULT,
            source_ref=None,
        )
    raise PolicyError(f"no policy assignment for agent {inputs.agent_id}")
