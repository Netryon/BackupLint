"""Config schema_version compatibility tests."""

from __future__ import annotations

import pytest

from backuplint.config import CONFIG_SCHEMA_VERSION, ConfigError, parse_config_data


def test_missing_schema_version_is_legacy_v1() -> None:
    cfg = parse_config_data({"backup_paths": ["/data"]})
    assert cfg.schema_version == CONFIG_SCHEMA_VERSION == 1


def test_explicit_schema_v1() -> None:
    cfg = parse_config_data({"schema_version": 1, "backup_paths": ["/data"]})
    assert cfg.schema_version == 1


def test_future_schema_rejected() -> None:
    with pytest.raises(ConfigError, match="Unsupported configuration schema_version"):
        parse_config_data({"schema_version": 2, "backup_paths": ["/data"]})


def test_malformed_schema_version() -> None:
    with pytest.raises(ConfigError, match="integer"):
        parse_config_data({"schema_version": "1", "backup_paths": ["/data"]})
    with pytest.raises(ConfigError, match="integer"):
        parse_config_data({"schema_version": True, "backup_paths": ["/data"]})
