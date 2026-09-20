"""Installed-vs-enabled diagnostics (never silently install)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from backuplint.install.detect import (
    DependencyCheck,
    DependencyPresence,
    detect_external_dependencies,
    detect_python_module,
)
from backuplint.install.features import FEATURE_REGISTRY, FeatureId, feature_definition


class DiagnosticSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class FeatureDiagnostic:
    feature: FeatureId
    severity: DiagnosticSeverity
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "feature": self.feature.value,
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class FeatureAvailability:
    feature: FeatureId
    installed: bool
    missing: tuple[str, ...]
    checks: tuple[DependencyCheck, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "feature": self.feature.value,
            "installed": self.installed,
            "missing": list(self.missing),
            "checks": [c.to_dict() for c in self.checks],
        }


def probe_feature_installed(
    feature: FeatureId,
    *,
    external: dict[str, DependencyCheck] | None = None,
) -> FeatureAvailability:
    """Determine whether support for a feature appears installed on this host.

    ``installed`` means required Python modules import and required external
    binaries are present. It does **not** mean the feature is enabled in config.
    """
    definition = feature_definition(feature)
    ext = external if external is not None else detect_external_dependencies()
    checks: list[DependencyCheck] = []
    missing: list[str] = []

    for module in definition.python_modules:
        check = detect_python_module(module)
        checks.append(check)
        if check.presence is not DependencyPresence.PRESENT:
            missing.append(module)

    for binary in definition.external_binaries:
        check = ext.get(binary) or detect_external_dependencies(names=(binary,))[binary]
        checks.append(check)
        if check.presence is not DependencyPresence.PRESENT:
            missing.append(binary)

    return FeatureAvailability(
        feature=feature,
        installed=not missing,
        missing=tuple(missing),
        checks=tuple(checks),
    )


def diagnose_enabled_vs_installed(
    *,
    enabled: dict[FeatureId, bool],
    availability: dict[FeatureId, FeatureAvailability] | None = None,
) -> list[FeatureDiagnostic]:
    """Produce diagnostics when runtime wants a feature that is not installed."""
    diagnostics: list[FeatureDiagnostic] = []
    avail = availability or {
        fid: probe_feature_installed(fid) for fid in FEATURE_REGISTRY
    }
    for fid, is_enabled in sorted(enabled.items(), key=lambda item: item[0].value):
        status = avail.get(fid) or probe_feature_installed(fid)
        if is_enabled and not status.installed:
            pretty = fid.value.replace("_", " ")
            missing = ", ".join(status.missing) if status.missing else "unknown support"
            diagnostics.append(
                FeatureDiagnostic(
                    feature=fid,
                    severity=DiagnosticSeverity.ERROR,
                    code="FEATURE_NOT_INSTALLED",
                    message=(
                        f"{pretty} requested but support is not installed "
                        f"(missing: {missing})"
                    ),
                )
            )
        elif (not is_enabled) and status.installed:
            diagnostics.append(
                FeatureDiagnostic(
                    feature=fid,
                    severity=DiagnosticSeverity.INFO,
                    code="INSTALLED_BUT_DISABLED",
                    message=(
                        f"{fid.value} support appears installed but is not enabled "
                        "in the current profile/config"
                    ),
                )
            )
    return diagnostics


def format_diagnostic_message(diagnostic: FeatureDiagnostic) -> str:
    return f"[{diagnostic.severity.value}] {diagnostic.code}: {diagnostic.message}"
