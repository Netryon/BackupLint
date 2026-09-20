"""Installer/profile rules for optional SIEM export."""

from __future__ import annotations

import pytest

from backuplint.install.features import FeatureId
from backuplint.install.profile import ProfileError, normalize_and_validate
from backuplint.install.roles import InstallationRole


def test_siem_optional_on_controller() -> None:
    profile = normalize_and_validate(
        {
            "schema_version": 1,
            "role": "controller",
            "deployment": "native",
            "features": {
                "fleet_controller": True,
                "siem_export": True,
            },
        }
    )
    assert profile.features[FeatureId.SIEM_EXPORT] is True
    assert profile.role is InstallationRole.CONTROLLER


def test_siem_defaults_off_on_standalone() -> None:
    profile = normalize_and_validate(
        {
            "schema_version": 1,
            "role": "standalone",
            "features": {"coverage": True, "integrity": True},
        }
    )
    assert profile.features[FeatureId.SIEM_EXPORT] is False


def test_agent_cannot_enable_siem_export() -> None:
    with pytest.raises(ProfileError, match="siem_export"):
        normalize_and_validate(
            {
                "schema_version": 1,
                "role": "agent",
                "features": {
                    "fleet_agent": True,
                    "siem_export": True,
                },
            }
        )


def test_all_in_one_can_select_siem_export() -> None:
    profile = normalize_and_validate(
        {
            "schema_version": 1,
            "role": "all_in_one",
            "features": {
                "fleet_controller": True,
                "siem_export": True,
                "coverage": True,
            },
        }
    )
    assert profile.features[FeatureId.SIEM_EXPORT] is True
