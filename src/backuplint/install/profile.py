"""Install profile schema: parse, validate, normalize, serialize."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backuplint.install.deployment import DeploymentForm, parse_deployment
from backuplint.install.features import (
    FEATURE_REGISTRY,
    FeatureId,
    default_feature_map,
    parse_feature_id,
)
from backuplint.install.roles import InstallationRole, parse_role
from backuplint.install.support import SupportAssessment, assess_support

PROFILE_SCHEMA_VERSION = 1


class ProfileError(Exception):
    """Invalid install profile."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class InstallProfile:
    """Validated, normalized installation profile (no secrets)."""

    schema_version: int
    role: InstallationRole
    deployment: DeploymentForm
    features: dict[FeatureId, bool]
    support: SupportAssessment

    def enabled_features(self) -> tuple[FeatureId, ...]:
        return tuple(fid for fid, enabled in sorted(self.features.items()) if enabled)

    def to_dict(self, *, include_support: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "role": self.role.value,
            "deployment": self.deployment.value,
            "features": {
                fid.value: enabled for fid, enabled in sorted(self.features.items())
            },
        }
        if include_support:
            data["support"] = {
                "architecture_valid": self.support.architecture_valid,
                "implementation": self.support.implementation.value,
                "detail": self.support.detail,
                "usable_now": self.support.usable_now,
            }
        return data

    def to_json(self, *, indent: int | None = 2, include_support: bool = True) -> str:
        return (
            json.dumps(
                self.to_dict(include_support=include_support),
                indent=indent,
                sort_keys=True,
            )
            + "\n"
        )


def _require_mapping(raw: object, *, what: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ProfileError(f"{what} must be a mapping/object")
    # Reject non-string keys early for deterministic errors.
    bad_keys = [k for k in raw if not isinstance(k, str)]
    if bad_keys:
        raise ProfileError(f"{what} keys must be strings")
    return dict(raw)


def normalize_and_validate(raw: object) -> InstallProfile:
    """Parse and validate a profile mapping. Never installs anything."""
    data = _require_mapping(raw, what="install profile")

    allowed_top = {"schema_version", "role", "deployment", "features"}
    unknown = sorted(set(data) - allowed_top)
    if unknown:
        raise ProfileError(
            "unknown profile field(s): " + ", ".join(repr(k) for k in unknown)
        )

    if "schema_version" not in data:
        raise ProfileError("schema_version is required")
    version_raw = data["schema_version"]
    if isinstance(version_raw, bool) or not isinstance(version_raw, int):
        raise ProfileError("schema_version must be an integer")
    if version_raw != PROFILE_SCHEMA_VERSION:
        raise ProfileError(
            f"unsupported profile schema_version {version_raw}; "
            f"this BackupLint supports version {PROFILE_SCHEMA_VERSION} only"
        )

    if "role" not in data:
        raise ProfileError("role is required")
    try:
        role = parse_role(data["role"])
    except ValueError as exc:
        raise ProfileError(str(exc)) from exc

    deployment_raw = data.get("deployment", DeploymentForm.NATIVE.value)
    try:
        deployment = parse_deployment(deployment_raw)
    except ValueError as exc:
        raise ProfileError(str(exc)) from exc

    features = default_feature_map(role)
    if "features" in data and data["features"] is not None:
        feature_map = _require_mapping(data["features"], what="features")
        for key, value in feature_map.items():
            try:
                fid = parse_feature_id(key)
            except ValueError as exc:
                raise ProfileError(str(exc)) from exc
            if not isinstance(value, bool):
                raise ProfileError(
                    f"features.{fid.value} must be a boolean (got {type(value).__name__})"
                )
            features[fid] = value

    _validate_feature_role_rules(role, features)
    support = assess_support(role, deployment)
    if not support.architecture_valid:
        raise ProfileError(
            f"unsupported role/deployment combination: {support.detail}"
        )

    # Deterministic key order in frozen dict via sorted construction.
    ordered = {fid: features[fid] for fid in FeatureId}
    return InstallProfile(
        schema_version=PROFILE_SCHEMA_VERSION,
        role=role,
        deployment=deployment,
        features=ordered,
        support=support,
    )


def _validate_feature_role_rules(
    role: InstallationRole, features: dict[FeatureId, bool]
) -> None:
    for fid, enabled in features.items():
        if not enabled:
            continue
        definition = FEATURE_REGISTRY[fid]
        if role not in definition.applicable_roles:
            raise ProfileError(
                f"feature {fid.value!r} is not applicable to role {role.value!r}"
            )

    if role is InstallationRole.AGENT and not features.get(FeatureId.FLEET_AGENT, False):
        raise ProfileError("role 'agent' requires features.fleet_agent: true")

    if role is InstallationRole.CONTROLLER and not features.get(
        FeatureId.FLEET_CONTROLLER, False
    ):
        raise ProfileError("role 'controller' requires features.fleet_controller: true")

    if role is InstallationRole.ALL_IN_ONE and not features.get(
        FeatureId.FLEET_CONTROLLER, False
    ):
        raise ProfileError(
            "role 'all_in_one' requires features.fleet_controller: true"
        )

    if role is InstallationRole.STANDALONE and features.get(
        FeatureId.FLEET_CONTROLLER, False
    ):
        raise ProfileError(
            "role 'standalone' cannot enable features.fleet_controller"
        )

    if role is InstallationRole.STANDALONE and features.get(FeatureId.FLEET_AGENT, False):
        raise ProfileError("role 'standalone' cannot enable features.fleet_agent")

    if features.get(FeatureId.FLEET_DASHBOARD, False):
        if not features.get(FeatureId.FLEET_CONTROLLER, False):
            raise ProfileError(
                "features.fleet_dashboard requires features.fleet_controller: true"
            )
        if role not in {InstallationRole.CONTROLLER, InstallationRole.ALL_IN_ONE}:
            raise ProfileError(
                f"feature 'fleet_dashboard' is not applicable to role {role.value!r}"
            )

    if role is InstallationRole.AGENT and features.get(FeatureId.SIEM_EXPORT, False):
        raise ProfileError(
            "feature 'siem_export' is not applicable to role 'agent'; "
            "agents never export directly to SIEM"
        )

    if role is InstallationRole.CONTROLLER:
        local = (
            features.get(FeatureId.COVERAGE, False)
            or features.get(FeatureId.INTEGRITY, False)
            or features.get(FeatureId.RESTORE_VERIFICATION, False)
            or features.get(FeatureId.SCHEDULER, False)
        )
        # Allowed explicitly, but scheduler without coverage is odd — still valid.
        _ = local

    if features.get(FeatureId.SCHEDULER, False):
        has_local_check = any(
            features.get(fid, False)
            for fid in (
                FeatureId.COVERAGE,
                FeatureId.INTEGRITY,
                FeatureId.RESTORE_VERIFICATION,
            )
        )
        if not has_local_check:
            raise ProfileError(
                "features.scheduler requires at least one of coverage, integrity, "
                "or restore_verification"
            )


def load_profile_file(path: Path) -> InstallProfile:
    """Load YAML or JSON profile from disk (extension-based)."""
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise ProfileError("PyYAML is required to load YAML profiles") from exc
        raw = yaml.safe_load(text)
    elif suffix == ".json":
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProfileError(f"invalid JSON profile: {exc}") from exc
    else:
        # Try JSON then YAML for extension-less / .profile files.
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            import yaml

            raw = yaml.safe_load(text)
    return normalize_and_validate(raw)


def profile_from_yaml(text: str) -> InstallProfile:
    import yaml

    return normalize_and_validate(yaml.safe_load(text))


def profile_from_json(text: str) -> InstallProfile:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProfileError(f"invalid JSON profile: {exc}") from exc
    return normalize_and_validate(raw)


def migrate_profile(raw: object) -> object:
    """Hook for future schema migrations.

    Currently only schema_version 1 is accepted; unknown versions fail in
    ``normalize_and_validate`` rather than being silently reinterpreted.
    """
    return raw
