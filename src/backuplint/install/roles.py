"""Installation roles for BackupLint (one product, role-aware install)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class InstallationRole(StrEnum):
    """Canonical installation roles.

    Installation chooses which components/dependencies exist on a host.
    Runtime configuration chooses which installed components are enabled.
    """

    STANDALONE = "standalone"
    AGENT = "agent"
    CONTROLLER = "controller"
    ALL_IN_ONE = "all_in_one"


@dataclass(frozen=True, slots=True)
class RoleSemantics:
    """Human- and machine-readable responsibility of a role."""

    role: InstallationRole
    summary: str
    local_verification: bool
    fleet_agent: bool
    fleet_controller: bool
    requires_compose_runtime: bool
    notes: tuple[str, ...]


ROLE_SEMANTICS: dict[InstallationRole, RoleSemantics] = {
    InstallationRole.STANDALONE: RoleSemantics(
        role=InstallationRole.STANDALONE,
        summary="Local BackupLint checks with optional scheduler; no fleet controller.",
        local_verification=True,
        fleet_agent=False,
        fleet_controller=False,
        requires_compose_runtime=True,
        notes=(
            "Typical single-host or per-server native install.",
            "Does not require a BackupLint controller.",
        ),
    ),
    InstallationRole.AGENT: RoleSemantics(
        role=InstallationRole.AGENT,
        summary="Local checks plus outbound fleet reporting to a controller.",
        local_verification=True,
        fleet_agent=True,
        fleet_controller=False,
        requires_compose_runtime=True,
        notes=(
            "Agent initiates outbound HTTPS/mTLS; no inbound agent port.",
            "Does not run a local fleet controller.",
        ),
    ),
    InstallationRole.CONTROLLER: RoleSemantics(
        role=InstallationRole.CONTROLLER,
        summary="Fleet controller host; local scan/Restic/Docker not required by default.",
        local_verification=False,
        fleet_agent=False,
        fleet_controller=True,
        requires_compose_runtime=False,
        notes=(
            "Controller does not imply multi-server topology by itself.",
            "Local verification features may be selected explicitly if desired.",
        ),
    ),
    InstallationRole.ALL_IN_ONE: RoleSemantics(
        role=InstallationRole.ALL_IN_ONE,
        summary="Local checks and fleet controller on the same host.",
        local_verification=True,
        fleet_agent=False,
        fleet_controller=True,
        requires_compose_runtime=True,
        notes=(
            "Useful for labs and small deployments.",
            "Still one product; components are co-located, not a separate SKU.",
        ),
    ),
}


def parse_role(raw: object) -> InstallationRole:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("role must be a non-empty string")
    key = raw.strip().lower().replace("-", "_")
    try:
        return InstallationRole(key)
    except ValueError as exc:
        allowed = ", ".join(r.value for r in InstallationRole)
        raise ValueError(f"unknown role {raw!r}; expected one of: {allowed}") from exc


def role_semantics(role: InstallationRole) -> RoleSemantics:
    return ROLE_SEMANTICS[role]
