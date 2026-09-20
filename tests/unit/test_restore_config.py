"""Unit tests for restore_verification config parsing."""

from __future__ import annotations

import pytest

from backuplint.config import ConfigError, RestoreVerificationMode, parse_config_data


def test_restore_verification_defaults_off() -> None:
    cfg = parse_config_data(
        {
            "backup_paths": [],
            "restic": {"repository": "/repo", "password_file": "/pw"},
        }
    )
    assert cfg.restic is not None
    assert cfg.restic.restore_verification.mode is RestoreVerificationMode.OFF


def test_restore_verification_selected_and_expected_paths(tmp_path) -> None:
    cfg = parse_config_data(
        {
            "backup_paths": [],
            "restic": {
                "repository": "/repo",
                "password_file": "pw",
                "restore_verification": {
                    "mode": "selected",
                    "timeout": "10m",
                    "expected_paths": ["config.json", "data/a"],
                },
            },
        },
        source_file=tmp_path / "backuplint.yml",
    )
    assert cfg.restic is not None
    rv = cfg.restic.restore_verification
    assert rv.mode is RestoreVerificationMode.SELECTED
    assert rv.timeout is not None
    assert rv.timeout.total_seconds() == 600
    assert rv.expected_paths == ("config.json", "data/a")


def test_restore_verification_rejects_unknown_mode_and_keys() -> None:
    with pytest.raises(ConfigError, match="off, selected, full"):
        parse_config_data(
            {
                "backup_paths": [],
                "restic": {
                    "repository": "/repo",
                    "restore_verification": {"mode": "deep"},
                },
            }
        )
    with pytest.raises(ConfigError, match="Unknown restic.restore_verification"):
        parse_config_data(
            {
                "backup_paths": [],
                "restic": {
                    "repository": "/repo",
                    "restore_verification": {"mode": "off", "nope": 1},
                },
            }
        )


def test_restore_verification_yaml_false_means_off() -> None:
    cfg = parse_config_data(
        {
            "backup_paths": [],
            "restic": {
                "repository": "/repo",
                "restore_verification": {"mode": False},
            },
        }
    )
    assert cfg.restic is not None
    assert cfg.restic.restore_verification.mode is RestoreVerificationMode.OFF
