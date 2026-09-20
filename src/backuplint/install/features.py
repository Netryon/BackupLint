"""Feature registry for role-aware BackupLint installation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from backuplint.install.roles import InstallationRole


class FeatureId(StrEnum):
    """Major installable/enableable BackupLint features.

    Names align with existing schedule/config terminology where possible.
    """

    COVERAGE = "coverage"
    INTEGRITY = "integrity"
    RESTORE_VERIFICATION = "restore_verification"
    SCHEDULER = "scheduler"
    FLEET_AGENT = "fleet_agent"
    FLEET_CONTROLLER = "fleet_controller"
    FLEET_DASHBOARD = "fleet_dashboard"
    SIEM_EXPORT = "siem_export"


class FeatureScope(StrEnum):
    LOCAL = "local"
    CONTROLLER = "controller"
    EITHER = "either"


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    feature_id: FeatureId
    summary: str
    applicable_roles: frozenset[InstallationRole]
    python_modules: tuple[str, ...]
    external_binaries: tuple[str, ...]
    optional_integrations: tuple[str, ...]
    needs_writable_state: bool
    scope: FeatureScope
    default_enabled_for: frozenset[InstallationRole]


FEATURE_REGISTRY: dict[FeatureId, FeatureDefinition] = {
    FeatureId.COVERAGE: FeatureDefinition(
        feature_id=FeatureId.COVERAGE,
        summary="Compose mount discovery and backup-path coverage checks.",
        applicable_roles=frozenset(
            {
                InstallationRole.STANDALONE,
                InstallationRole.AGENT,
                InstallationRole.ALL_IN_ONE,
                InstallationRole.CONTROLLER,  # only if explicitly selected
            }
        ),
        python_modules=("backuplint.compose", "backuplint.coverage", "backuplint.audit"),
        external_binaries=("docker",),
        optional_integrations=(),
        needs_writable_state=False,
        scope=FeatureScope.LOCAL,
        default_enabled_for=frozenset(
            {
                InstallationRole.STANDALONE,
                InstallationRole.AGENT,
                InstallationRole.ALL_IN_ONE,
            }
        ),
    ),
    FeatureId.INTEGRITY: FeatureDefinition(
        feature_id=FeatureId.INTEGRITY,
        summary="Restic repository integrity verification.",
        applicable_roles=frozenset(
            {
                InstallationRole.STANDALONE,
                InstallationRole.AGENT,
                InstallationRole.ALL_IN_ONE,
                InstallationRole.CONTROLLER,
            }
        ),
        python_modules=("backuplint.restic", "backuplint.integrity_state"),
        external_binaries=("restic",),
        optional_integrations=(),
        needs_writable_state=True,
        scope=FeatureScope.LOCAL,
        default_enabled_for=frozenset(
            {
                InstallationRole.STANDALONE,
                InstallationRole.AGENT,
                InstallationRole.ALL_IN_ONE,
            }
        ),
    ),
    FeatureId.RESTORE_VERIFICATION: FeatureDefinition(
        feature_id=FeatureId.RESTORE_VERIFICATION,
        summary="Selected-path restore verification against Restic snapshots.",
        applicable_roles=frozenset(
            {
                InstallationRole.STANDALONE,
                InstallationRole.AGENT,
                InstallationRole.ALL_IN_ONE,
                InstallationRole.CONTROLLER,
            }
        ),
        python_modules=("backuplint.restore_verify", "backuplint.restic"),
        external_binaries=("restic",),
        optional_integrations=(),
        needs_writable_state=True,
        scope=FeatureScope.LOCAL,
        default_enabled_for=frozenset(),
    ),
    FeatureId.SCHEDULER: FeatureDefinition(
        feature_id=FeatureId.SCHEDULER,
        summary="Local scheduled verification daemon.",
        applicable_roles=frozenset(
            {
                InstallationRole.STANDALONE,
                InstallationRole.AGENT,
                InstallationRole.ALL_IN_ONE,
            }
        ),
        python_modules=(
            "backuplint.scheduler",
            "backuplint.schedule_store",
            "backuplint.schedule_lock",
        ),
        external_binaries=(),
        optional_integrations=("systemd",),
        needs_writable_state=True,
        scope=FeatureScope.LOCAL,
        default_enabled_for=frozenset(
            {
                InstallationRole.STANDALONE,
                InstallationRole.AGENT,
                InstallationRole.ALL_IN_ONE,
            }
        ),
    ),
    FeatureId.FLEET_AGENT: FeatureDefinition(
        feature_id=FeatureId.FLEET_AGENT,
        summary="Outbound fleet agent enrollment and result submission.",
        applicable_roles=frozenset({InstallationRole.AGENT}),
        python_modules=("backuplint.fleet.agent", "backuplint.fleet.protocol"),
        external_binaries=(),
        optional_integrations=(),
        needs_writable_state=True,
        scope=FeatureScope.LOCAL,
        default_enabled_for=frozenset({InstallationRole.AGENT}),
    ),
    FeatureId.FLEET_CONTROLLER: FeatureDefinition(
        feature_id=FeatureId.FLEET_CONTROLLER,
        summary="HTTPS fleet controller for enrollment and result ingestion.",
        applicable_roles=frozenset(
            {InstallationRole.CONTROLLER, InstallationRole.ALL_IN_ONE}
        ),
        python_modules=(
            "backuplint.fleet.controller",
            "backuplint.fleet.controller_store",
            "backuplint.fleet.certs",
        ),
        external_binaries=("openssl",),
        optional_integrations=(),
        needs_writable_state=True,
        scope=FeatureScope.CONTROLLER,
        default_enabled_for=frozenset(
            {InstallationRole.CONTROLLER, InstallationRole.ALL_IN_ONE}
        ),
    ),
    FeatureId.FLEET_DASHBOARD: FeatureDefinition(
        feature_id=FeatureId.FLEET_DASHBOARD,
        summary="Optional read-only central web dashboard on the controller.",
        applicable_roles=frozenset(
            {InstallationRole.CONTROLLER, InstallationRole.ALL_IN_ONE}
        ),
        python_modules=(
            "backuplint.fleet.dashboard",
            "backuplint.fleet.dashboard.query",
            "backuplint.fleet.dashboard.http",
        ),
        external_binaries=(),
        optional_integrations=(),
        needs_writable_state=True,
        scope=FeatureScope.CONTROLLER,
        default_enabled_for=frozenset(),
    ),
    FeatureId.SIEM_EXPORT: FeatureDefinition(
        feature_id=FeatureId.SIEM_EXPORT,
        summary="Optional normalized SIEM export queue and HTTPS transport.",
        applicable_roles=frozenset(
            {
                InstallationRole.STANDALONE,
                InstallationRole.CONTROLLER,
                InstallationRole.ALL_IN_ONE,
            }
        ),
        python_modules=(
            "backuplint.siem",
            "backuplint.siem.exporter",
            "backuplint.siem.transport.https_json",
        ),
        external_binaries=(),
        optional_integrations=(),
        needs_writable_state=True,
        scope=FeatureScope.EITHER,
        default_enabled_for=frozenset(),
    ),
}


def feature_definition(feature_id: FeatureId) -> FeatureDefinition:
    return FEATURE_REGISTRY[feature_id]


def parse_feature_id(raw: object) -> FeatureId:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("feature id must be a non-empty string")
    key = raw.strip().lower()
    try:
        return FeatureId(key)
    except ValueError as exc:
        allowed = ", ".join(f.value for f in FeatureId)
        raise ValueError(f"unknown feature {raw!r}; expected one of: {allowed}") from exc


def default_feature_map(role: InstallationRole) -> dict[FeatureId, bool]:
    """Deterministic default enablement map for a role (all registry features)."""
    return {
        fid: (role in definition.default_enabled_for)
        for fid, definition in FEATURE_REGISTRY.items()
    }
