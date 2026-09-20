"""Role-aware installation and profile foundation.

This package plans and validates installation profiles. It never silently
downloads or installs missing dependencies.
"""

from __future__ import annotations

from backuplint.install.deployment import DeploymentForm
from backuplint.install.detect import DependencyCheck, detect_external_dependencies
from backuplint.install.diagnostics import (
    FeatureDiagnostic,
    diagnose_enabled_vs_installed,
    probe_feature_installed,
)
from backuplint.install.features import FeatureId
from backuplint.install.plan import InstallPlan, resolve_install_plan
from backuplint.install.profile import (
    PROFILE_SCHEMA_VERSION,
    InstallProfile,
    ProfileError,
    load_profile_file,
    normalize_and_validate,
    profile_from_json,
    profile_from_yaml,
)
from backuplint.install.roles import InstallationRole
from backuplint.install.support import ImplementationStatus, assess_support

__all__ = [
    "PROFILE_SCHEMA_VERSION",
    "DependencyCheck",
    "DeploymentForm",
    "FeatureDiagnostic",
    "FeatureId",
    "ImplementationStatus",
    "InstallPlan",
    "InstallProfile",
    "InstallationRole",
    "ProfileError",
    "assess_support",
    "detect_external_dependencies",
    "diagnose_enabled_vs_installed",
    "load_profile_file",
    "normalize_and_validate",
    "probe_feature_installed",
    "profile_from_json",
    "profile_from_yaml",
    "resolve_install_plan",
]
