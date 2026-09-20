"""Installer/profile integration with fleet foundations."""

from __future__ import annotations

import pytest

from backuplint.install.features import FeatureId
from backuplint.install.profile import ProfileError, normalize_and_validate
from backuplint.install.roles import ROLE_SEMANTICS, InstallationRole


def test_controller_role_does_not_require_restic_or_docker_features() -> None:
    profile = normalize_and_validate(
        {
            "schema_version": 1,
            "role": "controller",
            "deployment": "native",
            "features": {
                "fleet_controller": True,
                "coverage": False,
                "integrity": False,
                "restore_verification": False,
                "scheduler": False,
                "fleet_agent": False,
            },
        }
    )
    assert profile.role is InstallationRole.CONTROLLER
    assert profile.features[FeatureId.FLEET_CONTROLLER] is True
    assert profile.features[FeatureId.COVERAGE] is False
    assert profile.features[FeatureId.INTEGRITY] is False
    assert ROLE_SEMANTICS[InstallationRole.CONTROLLER].requires_compose_runtime is False


def test_agent_role_requires_fleet_agent_and_supports_enrollment() -> None:
    profile = normalize_and_validate(
        {
            "schema_version": 1,
            "role": "agent",
            "deployment": "native",
            "features": {
                "fleet_agent": True,
                "coverage": True,
                "integrity": True,
                "restore_verification": False,
                "scheduler": True,
                "fleet_controller": False,
            },
        }
    )
    assert profile.role is InstallationRole.AGENT
    assert profile.features[FeatureId.FLEET_AGENT] is True
    assert ROLE_SEMANTICS[InstallationRole.AGENT].fleet_agent is True
    assert profile.role.value == "agent"


def test_standalone_blocks_fleet_agent() -> None:
    with pytest.raises(ProfileError, match="standalone"):
        normalize_and_validate(
            {
                "schema_version": 1,
                "role": "standalone",
                "deployment": "native",
                "features": {"fleet_agent": True},
            }
        )
