"""Install-plan resolver: structured requirements, never installs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from backuplint.install.deployment import DeploymentForm
from backuplint.install.detect import (
    DependencyCheck,
    DependencyPresence,
    detect_external_dependencies,
)
from backuplint.install.diagnostics import (
    FeatureAvailability,
    FeatureDiagnostic,
    diagnose_enabled_vs_installed,
    probe_feature_installed,
)
from backuplint.install.features import FEATURE_REGISTRY, FeatureId, feature_definition
from backuplint.install.profile import InstallProfile
from backuplint.install.roles import ROLE_SEMANTICS, InstallationRole
from backuplint.install.support import ImplementationStatus


@dataclass(frozen=True, slots=True)
class InstallPlan:
    """Deterministic description of what a profile requires."""

    role: InstallationRole
    deployment: DeploymentForm
    selected_features: tuple[FeatureId, ...]
    project_components: tuple[str, ...]
    external_dependencies: tuple[str, ...]
    state_directories: tuple[str, ...]
    privilege_warnings: tuple[str, ...]
    missing_requirements: tuple[str, ...]
    unsupported_notes: tuple[str, ...]
    recommended_python_extras: tuple[str, ...]
    dependency_checks: tuple[DependencyCheck, ...] = ()
    feature_availability: tuple[FeatureAvailability, ...] = ()
    diagnostics: tuple[FeatureDiagnostic, ...] = ()
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "deployment": self.deployment.value,
            "selected_features": [f.value for f in self.selected_features],
            "project_components": list(self.project_components),
            "external_dependencies": list(self.external_dependencies),
            "state_directories": list(self.state_directories),
            "privilege_warnings": list(self.privilege_warnings),
            "missing_requirements": list(self.missing_requirements),
            "unsupported_notes": list(self.unsupported_notes),
            "recommended_python_extras": list(self.recommended_python_extras),
            "dependency_checks": [c.to_dict() for c in self.dependency_checks],
            "feature_availability": [a.to_dict() for a in self.feature_availability],
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "support": self.extras.get("support"),
            "role_summary": self.extras.get("role_summary"),
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True) + "\n"


def _state_dirs_for(features: tuple[FeatureId, ...]) -> tuple[str, ...]:
    dirs: list[str] = []
    if FeatureId.SCHEDULER in features:
        dirs.append("$XDG_DATA_HOME/backuplint/schedule or ~/.local/share/backuplint/schedule")
    if FeatureId.FLEET_AGENT in features:
        dirs.append("agent identity directory (client key/cert; mode 0700)")
    if FeatureId.FLEET_CONTROLLER in features:
        dirs.append("controller data directory (CA, SQLite, agent certs; mode 0700)")
    if FeatureId.INTEGRITY in features or FeatureId.RESTORE_VERIFICATION in features:
        dirs.append("optional integrity/restore state paths from backuplint.yml")
    return tuple(dirs)


def _privilege_warnings(
    role: InstallationRole,
    features: tuple[FeatureId, ...],
    deployment: DeploymentForm,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if FeatureId.COVERAGE in features or FeatureId.SCHEDULER in features:
        warnings.append(
            "Local verification may need permission to read Compose files and "
            "discover mounts (often via docker CLI)."
        )
    if FeatureId.RESTORE_VERIFICATION in features:
        warnings.append(
            "Restore verification writes to a configured destination; ensure the "
            "path is dedicated and not a host root."
        )
    if FeatureId.FLEET_CONTROLLER in features:
        warnings.append(
            "Controller TLS material (ca.key) is highly sensitive; protect the "
            "controller data directory."
        )
    if deployment is DeploymentForm.CONTAINER and role is InstallationRole.AGENT:
        warnings.append(
            "Containerized agents with host filesystem/Docker access are not a "
            "finished least-privilege model."
        )
    return tuple(warnings)


def _recommended_extras(features: tuple[FeatureId, ...]) -> tuple[str, ...]:
    """Internal recommended packaging split (not yet public pyproject extras)."""
    extras: list[str] = ["core"]
    local = {
        FeatureId.COVERAGE,
        FeatureId.INTEGRITY,
        FeatureId.RESTORE_VERIFICATION,
        FeatureId.SCHEDULER,
        FeatureId.FLEET_AGENT,
    }
    if any(f in features for f in local):
        extras.append("scan")
    if FeatureId.FLEET_CONTROLLER in features:
        extras.append("controller")
    if FeatureId.FLEET_AGENT in features:
        extras.append("agent")
    return tuple(dict.fromkeys(extras))


def resolve_install_plan(
    profile: InstallProfile,
    *,
    probe_host: bool = True,
    external_checks: dict[str, DependencyCheck] | None = None,
) -> InstallPlan:
    """Build a structured install plan from a validated profile.

    Never downloads or installs packages/binaries.
    """
    selected = profile.enabled_features()
    modules: list[str] = []
    binaries: list[str] = []
    for fid in selected:
        definition = feature_definition(fid)
        modules.extend(definition.python_modules)
        binaries.extend(definition.external_binaries)
    # Deterministic unique order
    project_components = tuple(dict.fromkeys(modules))
    external_dependencies = tuple(dict.fromkeys(binaries))

    unsupported_notes: list[str] = []
    if profile.support.implementation is ImplementationStatus.PLANNED:
        unsupported_notes.append(profile.support.detail)
    elif profile.support.implementation is ImplementationStatus.LIMITED:
        unsupported_notes.append(profile.support.detail)
    elif profile.support.implementation is ImplementationStatus.UNSUPPORTED:
        unsupported_notes.append(profile.support.detail)

    checks_map = external_checks
    if probe_host:
        known = ("restic", "docker", "openssl")
        probe_names = tuple(n for n in external_dependencies if n in known) or known
        checks_map = detect_external_dependencies(names=probe_names)
    elif checks_map is None:
        checks_map = {}

    availability: list[FeatureAvailability] = []
    enabled_map = {fid: (fid in selected) for fid in FEATURE_REGISTRY}
    if probe_host or external_checks is not None:
        for fid in FEATURE_REGISTRY:
            availability.append(
                probe_feature_installed(fid, external=checks_map or {})
            )
        diagnostics = diagnose_enabled_vs_installed(
            enabled={fid: fid in selected for fid in FeatureId},
            availability={a.feature: a for a in availability},
        )
    else:
        diagnostics = []

    missing: list[str] = []
    for name in external_dependencies:
        check = (checks_map or {}).get(name)
        if check is not None and check.presence is DependencyPresence.ABSENT:
            missing.append(name)
        elif probe_host and check is None:
            # Unknown detector — report as informational missing probe.
            missing.append(f"{name} (no detector)")

    # Feature-level missing support for enabled features.
    for diag in diagnostics:
        if diag.code == "FEATURE_NOT_INSTALLED":
            missing.append(diag.message)

    semantics = ROLE_SEMANTICS[profile.role]
    return InstallPlan(
        role=profile.role,
        deployment=profile.deployment,
        selected_features=selected,
        project_components=project_components,
        external_dependencies=external_dependencies,
        state_directories=_state_dirs_for(selected),
        privilege_warnings=_privilege_warnings(
            profile.role, selected, profile.deployment
        ),
        missing_requirements=tuple(dict.fromkeys(missing)),
        unsupported_notes=tuple(unsupported_notes),
        recommended_python_extras=_recommended_extras(selected),
        dependency_checks=tuple(checks_map.values()) if checks_map else (),
        feature_availability=tuple(availability),
        diagnostics=tuple(diagnostics),
        extras={
            "support": {
                "architecture_valid": profile.support.architecture_valid,
                "implementation": profile.support.implementation.value,
                "detail": profile.support.detail,
                "usable_now": profile.support.usable_now,
            },
            "role_summary": semantics.summary,
            "enabled_map": {k.value: v for k, v in enabled_map.items()},
        },
    )


def plan_to_json(plan: InstallPlan, *, indent: int | None = 2) -> str:
    return plan.to_json(indent=indent)
