"""Configuration torture tests for production readiness."""

from __future__ import annotations

from pathlib import Path

import pytest

from backuplint.config import ConfigError, load_config, parse_config_data


def test_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "backuplint.yml"
    path.write_text("")
    with pytest.raises(ConfigError, match="empty|mapping|backup_paths"):
        load_config(path)


def test_null_backup_paths() -> None:
    # Explicit null is treated as empty list by parser helper.
    config = parse_config_data({"backup_paths": None})
    assert config.backup_paths == ()


def test_wrong_types() -> None:
    with pytest.raises(ConfigError):
        parse_config_data({"backup_paths": 12})
    with pytest.raises(ConfigError):
        parse_config_data({"backup_paths": [1, 2]})
    with pytest.raises(ConfigError):
        parse_config_data({"backup_paths": [], "restic": "nope"})


def test_whitespace_only_path_rejected() -> None:
    with pytest.raises(ConfigError, match="non-empty"):
        parse_config_data({"backup_paths": ["\t  "]})


def test_invalid_max_backup_age() -> None:
    with pytest.raises(ConfigError, match="max_backup_age"):
        parse_config_data({"backup_paths": [], "max_backup_age": "soon"})


def test_zero_duration_rejected_or_accepted() -> None:
    # Zero is not a useful freshness window; parser should reject empty/invalid.
    with pytest.raises(ConfigError):
        parse_config_data(
            {
                "backup_paths": [],
                "restic": {"repository": "/repo"},
                "max_backup_age": "0s",
            }
        )


def test_large_duration(tmp_path: Path) -> None:
    path = tmp_path / "backuplint.yml"
    path.write_text(
        "backup_paths: []\n"
        "restic:\n"
        "  repository: /repo\n"
        "max_backup_age: 3650d\n"
    )
    config = load_config(path)
    assert config.max_backup_age is not None
    assert config.max_backup_age.total_seconds() == 3650 * 24 * 3600


def test_max_backup_age_without_restic() -> None:
    with pytest.raises(ConfigError, match="requires a 'restic'"):
        parse_config_data({"backup_paths": [], "max_backup_age": "1h"})


def test_relative_and_absolute_paths(tmp_path: Path) -> None:
    path = tmp_path / "backuplint.yml"
    path.write_text("backup_paths:\n  - ./data\n  - /srv/docker\n")
    config = load_config(path)
    assert config.backup_paths == ("./data", "/srv/docker")


def test_invalid_password_file_type() -> None:
    with pytest.raises(ConfigError):
        parse_config_data(
            {
                "backup_paths": [],
                "restic": {"repository": "/repo", "password_file": 3},
            }
        )


def test_shell_metacharacters_remain_data() -> None:
    config = parse_config_data(
        {"backup_paths": ["/srv/app; rm -rf /", "/srv/app && true", "/srv/$HOME"]}
    )
    assert config.backup_paths[0].endswith("; rm -rf /")
    assert "&&" in config.backup_paths[1]
