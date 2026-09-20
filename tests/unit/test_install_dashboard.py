"""Installer/profile rules for optional fleet dashboard."""

from __future__ import annotations

import pytest

from backuplint.install.features import FeatureId
from backuplint.install.profile import ProfileError, normalize_and_validate
from backuplint.install.roles import InstallationRole


def test_dashboard_optional_on_controller() -> None:
    profile = normalize_and_validate(
        {
            "schema_version": 1,
            "role": "controller",
            "deployment": "native",
            "features": {
                "fleet_controller": True,
                "fleet_dashboard": True,
                "coverage": False,
                "integrity": False,
            },
        }
    )
    assert profile.features[FeatureId.FLEET_DASHBOARD] is True
    assert profile.role is InstallationRole.CONTROLLER


def test_dashboard_defaults_off_on_controller() -> None:
    profile = normalize_and_validate(
        {
            "schema_version": 1,
            "role": "controller",
            "features": {"fleet_controller": True},
        }
    )
    assert profile.features[FeatureId.FLEET_DASHBOARD] is False


def test_agent_cannot_enable_dashboard() -> None:
    with pytest.raises(ProfileError, match="fleet_dashboard"):
        normalize_and_validate(
            {
                "schema_version": 1,
                "role": "agent",
                "features": {
                    "fleet_agent": True,
                    "fleet_dashboard": True,
                },
            }
        )


def test_standalone_does_not_require_dashboard() -> None:
    profile = normalize_and_validate(
        {
            "schema_version": 1,
            "role": "standalone",
            "features": {"coverage": True, "integrity": True, "scheduler": True},
        }
    )
    assert profile.features[FeatureId.FLEET_DASHBOARD] is False


def test_all_in_one_can_select_dashboard() -> None:
    profile = normalize_and_validate(
        {
            "schema_version": 1,
            "role": "all_in_one",
            "features": {
                "fleet_controller": True,
                "fleet_dashboard": True,
                "coverage": True,
                "integrity": True,
                "scheduler": True,
            },
        }
    )
    assert profile.features[FeatureId.FLEET_DASHBOARD] is True
