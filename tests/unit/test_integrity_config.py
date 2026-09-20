"""Unit tests for Restic integrity configuration parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from backuplint.config import (
    ConfigError,
    IntegrityMode,
    load_config,
    parse_config_data,
    parse_integrity_mode,
)


def test_default_integrity_mode_is_off() -> None:
    config = parse_config_data(
        {"backup_paths": [], "restic": {"repository": "/repo"}}
    )
    assert config.restic is not None
    assert config.restic.integrity.mode is IntegrityMode.OFF
    assert config.restic.integrity.max_age is None
    assert config.restic.integrity.state_file is None


def test_integrity_modes(tmp_path: Path) -> None:
    for mode in ("off", "standard", "deep"):
        path = tmp_path / f"{mode}.yml"
        path.write_text(
            "backup_paths: []\n"
            "restic:\n"
            "  repository: /repo\n"
            "  integrity:\n"
            f"    mode: {mode}\n"
        )
        config = load_config(path)
        assert config.restic is not None
        assert config.restic.integrity.mode is IntegrityMode(mode)


def test_integrity_mode_case_insensitive() -> None:
    config = parse_config_data(
        {
            "backup_paths": [],
            "restic": {
                "repository": "/repo",
                "integrity": {"mode": "Standard"},
            },
        }
    )
    assert config.restic is not None
    assert config.restic.integrity.mode is IntegrityMode.STANDARD


def test_invalid_integrity_mode_rejected() -> None:
    with pytest.raises(ConfigError, match="restic.integrity.mode"):
        parse_config_data(
            {
                "backup_paths": [],
                "restic": {
                    "repository": "/repo",
                    "integrity": {"mode": "FULL"},
                },
            }
        )


def test_integrity_must_be_mapping() -> None:
    with pytest.raises(ConfigError, match="must be a mapping"):
        parse_config_data(
            {
                "backup_paths": [],
                "restic": {"repository": "/repo", "integrity": "standard"},
            }
        )


def test_unknown_integrity_keys_rejected() -> None:
    with pytest.raises(ConfigError, match="Unknown restic.integrity key"):
        parse_config_data(
            {
                "backup_paths": [],
                "restic": {
                    "repository": "/repo",
                    "integrity": {"mode": "standard", "repair": True},
                },
            }
        )


def test_invalid_integrity_max_age() -> None:
    with pytest.raises(ConfigError, match="Invalid restic.integrity.max_age"):
        parse_config_data(
            {
                "backup_paths": [],
                "restic": {
                    "repository": "/repo",
                    "integrity": {"mode": "standard", "max_age": "soon"},
                },
            }
        )


def test_zero_integrity_max_age_rejected() -> None:
    with pytest.raises(ConfigError, match="restic.integrity.max_age"):
        parse_config_data(
            {
                "backup_paths": [],
                "restic": {
                    "repository": "/repo",
                    "integrity": {"mode": "standard", "max_age": "0s"},
                },
            }
        )


def test_integrity_state_file_relative_and_absolute(tmp_path: Path) -> None:
    path = tmp_path / "backuplint.yml"
    path.write_text(
        "backup_paths: []\n"
        "restic:\n"
        "  repository: /repo\n"
        "  integrity:\n"
        "    mode: standard\n"
        "    max_age: 7d\n"
        "    state_file: ./.backuplint-integrity.json\n"
    )
    config = load_config(path)
    assert config.restic is not None
    assert config.restic.integrity.max_age is not None
    assert config.restic.integrity.max_age.total_seconds() == 7 * 24 * 3600
    assert config.restic.integrity.state_file == (
        tmp_path / ".backuplint-integrity.json"
    ).resolve()

    absolute = parse_config_data(
        {
            "backup_paths": [],
            "restic": {
                "repository": "/repo",
                "integrity": {
                    "mode": "deep",
                    "state_file": "/var/lib/backuplint/state.json",
                },
            },
        },
        source_file=path,
    )
    assert absolute.restic is not None
    assert absolute.restic.integrity.state_file == Path(
        "/var/lib/backuplint/state.json"
    )


def test_integrity_mode_yaml_unquoted_off(tmp_path: Path) -> None:
    """Unquoted YAML `off` becomes False; accept it as IntegrityMode.OFF."""
    path = tmp_path / "backuplint.yml"
    path.write_text(
        "backup_paths: []\n"
        "restic:\n"
        "  repository: /repo\n"
        "  integrity:\n"
        "    mode: off\n"
    )
    config = load_config(path)
    assert config.restic is not None
    assert config.restic.integrity.mode is IntegrityMode.OFF


def test_parse_integrity_mode_helper() -> None:
    assert parse_integrity_mode("deep") is IntegrityMode.DEEP
    with pytest.raises(ConfigError, match="Integrity mode"):
        parse_integrity_mode("nope")
