"""Installation profile foundation tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from backuplint.install.deployment import DeploymentForm
from backuplint.install.detect import (
    DependencyCheck,
    DependencyPresence,
    detect_external_dependencies,
)
from backuplint.install.diagnostics import (
    DiagnosticSeverity,
    diagnose_enabled_vs_installed,
    format_diagnostic_message,
    probe_feature_installed,
)
from backuplint.install.features import FeatureId, default_feature_map
from backuplint.install.plan import resolve_install_plan
from backuplint.install.profile import (
    PROFILE_SCHEMA_VERSION,
    ProfileError,
    load_profile_file,
    migrate_profile,
    normalize_and_validate,
    profile_from_json,
    profile_from_yaml,
)
from backuplint.install.roles import InstallationRole, role_semantics
from backuplint.install.support import ImplementationStatus, assess_support


def test_role_semantics_cover_all_roles() -> None:
    for role in InstallationRole:
        semantics = role_semantics(role)
        assert semantics.role is role
        assert semantics.summary


@pytest.mark.parametrize(
    ("role", "deployment", "implementation"),
    [
        (InstallationRole.STANDALONE, DeploymentForm.NATIVE, ImplementationStatus.IMPLEMENTED),
        (InstallationRole.AGENT, DeploymentForm.NATIVE, ImplementationStatus.IMPLEMENTED),
        (InstallationRole.CONTROLLER, DeploymentForm.NATIVE, ImplementationStatus.IMPLEMENTED),
        (InstallationRole.ALL_IN_ONE, DeploymentForm.NATIVE, ImplementationStatus.IMPLEMENTED),
        (InstallationRole.CONTROLLER, DeploymentForm.CONTAINER, ImplementationStatus.PLANNED),
        (InstallationRole.AGENT, DeploymentForm.CONTAINER, ImplementationStatus.LIMITED),
        (InstallationRole.STANDALONE, DeploymentForm.CONTAINER, ImplementationStatus.UNSUPPORTED),
    ],
)
def test_support_matrix(
    role: InstallationRole,
    deployment: DeploymentForm,
    implementation: ImplementationStatus,
) -> None:
    assessment = assess_support(role, deployment)
    assert assessment.implementation is implementation
    assert assessment.architecture_valid is True


def test_all_in_one_container_architecture_invalid() -> None:
    assessment = assess_support(
        InstallationRole.ALL_IN_ONE, DeploymentForm.CONTAINER
    )
    assert assessment.architecture_valid is False
    assert assessment.implementation is ImplementationStatus.UNSUPPORTED
    with pytest.raises(ProfileError, match="unsupported role/deployment"):
        normalize_and_validate(
            {
                "schema_version": 1,
                "role": "all_in_one",
                "deployment": "container",
            }
        )


def test_agent_profile_yaml_round_trip() -> None:
    text = """
schema_version: 1
role: agent
deployment: native
features:
  coverage: true
  integrity: true
  restore_verification: false
  scheduler: true
"""
    profile = profile_from_yaml(text)
    assert profile.role is InstallationRole.AGENT
    assert profile.features[FeatureId.FLEET_AGENT] is True
    assert profile.features[FeatureId.FLEET_CONTROLLER] is False
    again = normalize_and_validate(profile.to_dict())
    assert again.features == profile.features
    assert again.role is profile.role


def test_json_and_file_loaders(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "role": "controller",
        "deployment": "native",
        "features": {"fleet_controller": True, "coverage": False},
    }
    json_path = tmp_path / "c.json"
    yaml_path = tmp_path / "c.yaml"
    json_path.write_text(json.dumps(payload), encoding="utf-8")
    yaml_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    assert load_profile_file(json_path).role is InstallationRole.CONTROLLER
    assert load_profile_file(yaml_path).role is InstallationRole.CONTROLLER
    assert profile_from_json(json.dumps(payload)).deployment is DeploymentForm.NATIVE


@pytest.mark.parametrize(
    "raw",
    [
        {"schema_version": 2, "role": "agent"},
        {"schema_version": "1", "role": "agent"},
        {"schema_version": 1, "role": "edge"},
        {"schema_version": 1, "role": "agent", "deployment": "unikernel"},
        {"schema_version": 1, "role": "agent", "features": {"telepathy": True}},
        {"schema_version": 1, "role": "agent", "shell": "rm -rf /"},
        {"schema_version": 1, "role": "standalone", "features": {"fleet_controller": True}},
        {"schema_version": 1, "role": "agent", "features": {"fleet_agent": False}},
        {
            "schema_version": 1,
            "role": "standalone",
            "features": {
                "scheduler": True,
                "coverage": False,
                "integrity": False,
                "restore_verification": False,
            },
        },
    ],
)
def test_invalid_profiles_rejected(raw: dict[str, object]) -> None:
    with pytest.raises(ProfileError):
        normalize_and_validate(raw)


def test_schema_migration_hook_is_passthrough() -> None:
    raw = {"schema_version": 1, "role": "standalone"}
    assert migrate_profile(raw) is raw


def test_controller_defaults_exclude_local_scan() -> None:
    defaults = default_feature_map(InstallationRole.CONTROLLER)
    assert defaults[FeatureId.FLEET_CONTROLLER] is True
    assert defaults[FeatureId.COVERAGE] is False
    assert defaults[FeatureId.SCHEDULER] is False
    profile = normalize_and_validate({"schema_version": 1, "role": "controller"})
    plan = resolve_install_plan(profile, probe_host=False)
    assert FeatureId.FLEET_CONTROLLER in plan.selected_features
    assert "openssl" in plan.external_dependencies
    assert "docker" not in plan.external_dependencies
    assert "controller" in plan.recommended_python_extras


def test_standalone_and_all_in_one_plans() -> None:
    standalone = normalize_and_validate({"schema_version": 1, "role": "standalone"})
    plan = resolve_install_plan(standalone, probe_host=False)
    assert "docker" in plan.external_dependencies
    assert FeatureId.FLEET_CONTROLLER not in plan.selected_features

    aio = normalize_and_validate({"schema_version": 1, "role": "all_in_one"})
    aio_plan = resolve_install_plan(aio, probe_host=False)
    assert FeatureId.FLEET_CONTROLLER in aio_plan.selected_features
    assert "docker" in aio_plan.external_dependencies
    assert "openssl" in aio_plan.external_dependencies


def test_plan_reports_missing_without_installing() -> None:
    profile = normalize_and_validate(
        {
            "schema_version": 1,
            "role": "standalone",
            "features": {
                "coverage": True,
                "integrity": True,
                "restore_verification": True,
                "scheduler": True,
            },
        }
    )
    fake = {
        "docker": DependencyCheck(
            name="docker",
            presence=DependencyPresence.ABSENT,
            version=None,
            check="test",
            detail="absent",
        ),
        "restic": DependencyCheck(
            name="restic",
            presence=DependencyPresence.ABSENT,
            version=None,
            check="test",
            detail="absent",
        ),
        "openssl": DependencyCheck(
            name="openssl",
            presence=DependencyPresence.PRESENT,
            version="3.0.0",
            check="test",
            detail="present",
        ),
    }
    plan = resolve_install_plan(profile, probe_host=False, external_checks=fake)
    assert "docker" in plan.missing_requirements
    assert "restic" in plan.missing_requirements
    assert plan.to_dict()["role"] == "standalone"
    json.loads(plan.to_json())


def test_installed_vs_enabled_diagnostics() -> None:
    availability = {
        FeatureId.RESTORE_VERIFICATION: probe_feature_installed(
            FeatureId.RESTORE_VERIFICATION,
            external={
                "restic": DependencyCheck(
                    name="restic",
                    presence=DependencyPresence.ABSENT,
                    version=None,
                    check="t",
                    detail="missing",
                )
            },
        ),
        FeatureId.SCHEDULER: probe_feature_installed(
            FeatureId.SCHEDULER, external={}
        ),
    }
    diags = diagnose_enabled_vs_installed(
        enabled={
            FeatureId.RESTORE_VERIFICATION: True,
            FeatureId.SCHEDULER: False,
        },
        availability=availability,
    )
    codes = {d.code for d in diags}
    assert "FEATURE_NOT_INSTALLED" in codes
    not_installed = [d for d in diags if d.code == "FEATURE_NOT_INSTALLED"][0]
    assert not_installed.severity is DiagnosticSeverity.ERROR
    assert "restore verification requested but support is not installed" in not_installed.message
    assert "FEATURE_NOT_INSTALLED" in format_diagnostic_message(not_installed)
    if availability[FeatureId.SCHEDULER].installed:
        assert "INSTALLED_BUT_DISABLED" in codes


def test_agent_container_limited_but_valid() -> None:
    profile = normalize_and_validate(
        {"schema_version": 1, "role": "agent", "deployment": "container"}
    )
    assert profile.support.implementation is ImplementationStatus.LIMITED
    assert profile.support.usable_now is True
    plan = resolve_install_plan(profile, probe_host=False)
    assert plan.unsupported_notes
    assert any("least-privilege" in w for w in plan.privilege_warnings)


def test_detect_external_dependencies_non_destructive() -> None:
    results = detect_external_dependencies(names=("openssl",))
    assert "openssl" in results
    assert results["openssl"].presence in {
        DependencyPresence.PRESENT,
        DependencyPresence.ABSENT,
        DependencyPresence.UNKNOWN,
    }
    assert results["openssl"].check


def test_controller_explicit_local_features_allowed() -> None:
    profile = normalize_and_validate(
        {
            "schema_version": 1,
            "role": "controller",
            "features": {
                "fleet_controller": True,
                "coverage": True,
                "integrity": False,
                "scheduler": False,
            },
        }
    )
    assert profile.features[FeatureId.COVERAGE] is True
    plan = resolve_install_plan(profile, probe_host=False)
    assert "docker" in plan.external_dependencies
    assert "openssl" in plan.external_dependencies


def test_deterministic_normalization_feature_order() -> None:
    profile = normalize_and_validate({"schema_version": 1, "role": "agent"})
    keys = list(profile.to_dict()["features"])
    assert keys == sorted(keys)
    assert profile.schema_version == PROFILE_SCHEMA_VERSION


def test_cli_module_source_avoids_eager_restic_imports() -> None:
    """Controller-friendly CLI should not top-level import scan/Restic code."""
    source = Path(__file__).resolve().parents[2] / "src" / "backuplint" / "cli.py"
    text = source.read_text(encoding="utf-8")
    # Top-level import block ends before first command; ensure no eager restic/compose.
    header = text.split("@app.callback", 1)[0]
    assert "from backuplint.restic" not in header
    assert "from backuplint.compose" not in header
    assert "from backuplint.audit" not in header
    assert "backuplint.install" in text or "profile_app" in text


def test_install_package_has_no_runtime_scan_imports() -> None:
    import backuplint.install as install_pkg

    source_dir = Path(install_pkg.__file__).resolve().parent
    for path in source_dir.glob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert not stripped.startswith("from backuplint.restic")
            assert not stripped.startswith("import backuplint.restic")
            assert not stripped.startswith("from backuplint.compose")
            assert not stripped.startswith("from backuplint.audit")
            assert "import backuplint.compose" not in stripped
            assert "import backuplint.audit" not in stripped
