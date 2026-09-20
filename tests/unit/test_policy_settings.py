"""Policy settings validation and secret rejection."""

from __future__ import annotations

import pytest

from backuplint.policy.schema import PolicyError, build_policy_snapshot
from backuplint.policy.settings import validate_policy_settings


def test_allowed_settings() -> None:
    settings = {
        "schedule": {"coverage": {"enabled": True, "every": "30m"}},
        "siem": {"enabled": False},
        "queue": {"max_items": 200},
        "reporting": {"heartbeat_interval": "60s"},
    }
    out = validate_policy_settings(settings)
    assert out["schedule"]["coverage"]["enabled"] is True


def test_reject_unknown_top_level() -> None:
    with pytest.raises(PolicyError, match="unknown"):
        validate_policy_settings({"docker": {"enabled": True}})


def test_reject_raw_secret_field() -> None:
    with pytest.raises(PolicyError):
        validate_policy_settings({"schedule": {"password": "secret"}})


def test_reject_shell_metacharacters() -> None:
    with pytest.raises(PolicyError):
        build_policy_snapshot(
            policy_id="pol-x",
            created_by="t",
            display_name="x",
            settings={
                "siem": {"enabled": True, "endpoint": "https://example.com;rm -rf /"}
            },
        )


def test_secret_ref_allowed() -> None:
    snap = build_policy_snapshot(
        policy_id="pol-ref",
        created_by="t",
        display_name="x",
        settings={
            "siem": {
                "enabled": True,
                "auth_token": {"source": "env", "name": "SIEM_TOKEN"},
            }
        },
    )
    assert "auth_token" in snap.settings["siem"]
