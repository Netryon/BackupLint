"""Unit tests for BackupLint configuration loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from backuplint.config import ConfigError, load_config, parse_config_data


def test_load_valid_config(tmp_path: Path) -> None:
    path = tmp_path / "backuplint.yml"
    path.write_text("backup_paths:\n  - /srv/docker\n  - /srv/config\n")
    config = load_config(path)
    assert config.backup_paths == ("/srv/docker", "/srv/config")


def test_duplicate_entries_are_deduplicated(tmp_path: Path) -> None:
    path = tmp_path / "backuplint.yml"
    path.write_text("backup_paths:\n  - /srv/docker\n  - /srv/docker\n")
    config = load_config(path)
    assert config.backup_paths == ("/srv/docker",)


def test_blank_list_allowed(tmp_path: Path) -> None:
    path = tmp_path / "backuplint.yml"
    path.write_text("backup_paths: []\n")
    config = load_config(path)
    assert config.backup_paths == ()


def test_missing_backup_paths_key() -> None:
    with pytest.raises(ConfigError, match="backup_paths"):
        parse_config_data({})


def test_malformed_yaml(tmp_path: Path) -> None:
    path = tmp_path / "backuplint.yml"
    path.write_text("backup_paths: [\n  - /srv/docker\n")
    with pytest.raises(ConfigError, match="Malformed"):
        load_config(path)


def test_non_list_backup_paths() -> None:
    with pytest.raises(ConfigError, match="must be a list"):
        parse_config_data({"backup_paths": "/srv/docker"})


def test_empty_string_entry_rejected() -> None:
    with pytest.raises(ConfigError, match="non-empty"):
        parse_config_data({"backup_paths": ["  "]})


def test_unknown_keys_rejected() -> None:
    with pytest.raises(ConfigError, match="Unknown configuration key"):
        parse_config_data({"backup_paths": [], "telemetry": True})


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yml")


def test_restic_config_parsed(tmp_path: Path) -> None:
    path = tmp_path / "backuplint.yml"
    path.write_text(
        "backup_paths: []\n"
        "max_backup_age: 24h\n"
        "restic:\n"
        "  repository: /var/backups/restic\n"
        "  password_file: ./restic.pass\n"
    )
    config = load_config(path)
    assert config.restic is not None
    assert config.restic.repository == "/var/backups/restic"
    assert config.restic.password_file == (tmp_path / "restic.pass").resolve()
    assert config.max_backup_age is not None
    assert config.max_backup_age.total_seconds() == 24 * 3600


def test_max_backup_age_requires_restic() -> None:
    with pytest.raises(ConfigError, match="requires a 'restic'"):
        parse_config_data({"backup_paths": [], "max_backup_age": "24h"})


def test_invalid_max_backup_age() -> None:
    with pytest.raises(ConfigError, match="Invalid max_backup_age"):
        parse_config_data(
            {
                "backup_paths": [],
                "max_backup_age": "soon",
                "restic": {"repository": "/repo"},
            }
        )


def test_restic_unknown_key_rejected() -> None:
    with pytest.raises(ConfigError, match="Unknown restic key"):
        parse_config_data(
            {
                "backup_paths": [],
                "restic": {"repository": "/repo", "password_cmd": "nope"},
            }
        )


def test_restic_password_inline_string_rejected() -> None:
    with pytest.raises(ConfigError, match="mapping"):
        parse_config_data(
            {
                "backup_paths": [],
                "restic": {"repository": "/repo", "password": "nope"},
            }
        )
