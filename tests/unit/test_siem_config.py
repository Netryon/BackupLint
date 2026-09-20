"""SIEM config parsing and validation tests."""

from __future__ import annotations

import pytest

from backuplint.secrets import InvalidSecretRefError
from backuplint.siem.config import (
    SIEM_CONFIG_SCHEMA_VERSION,
    SiemConfigError,
    parse_siem_config,
)
from backuplint.siem.event import SiemEventFamily, SiemSeverity


def test_defaults_disabled() -> None:
    cfg = parse_siem_config(None)
    assert cfg.enabled is False
    assert cfg.schema_version == SIEM_CONFIG_SCHEMA_VERSION


def test_enabled_requires_endpoint_and_bearer_token() -> None:
    with pytest.raises(SiemConfigError, match="endpoint"):
        parse_siem_config({"enabled": True, "siem_config_schema_version": 1})
    cfg = parse_siem_config(
        {
            "siem_config_schema_version": 1,
            "enabled": True,
            "endpoint": "https://siem.example/ingest",
            "auth": {
                "type": "bearer",
                "token": {"source": "env", "name": "BACKUPLINT_SIEM_TOKEN"},
            },
        }
    )
    assert cfg.enabled is True
    assert cfg.auth_token is not None
    assert cfg.auth_token.name == "BACKUPLINT_SIEM_TOKEN"


def test_schema_version_mismatch_fails_closed() -> None:
    with pytest.raises(SiemConfigError, match="unsupported siem_config_schema_version"):
        parse_siem_config({"siem_config_schema_version": 99})


def test_raw_token_string_rejected() -> None:
    with pytest.raises(InvalidSecretRefError):
        parse_siem_config(
            {
                "siem_config_schema_version": 1,
                "enabled": True,
                "endpoint": "https://siem.example/ingest",
                "auth": {"type": "bearer", "token": "raw-secret"},
            }
        )


def test_families_filter_cannot_exclude_critical() -> None:
    with pytest.raises(SiemConfigError, match="cannot exclude critical/high"):
        parse_siem_config(
            {
                "siem_config_schema_version": 1,
                "filters": {
                    "families": ["agent_online"],
                },
            }
        )


def test_unsupported_transport_rejected() -> None:
    with pytest.raises(SiemConfigError, match="not supported in v0.7"):
        parse_siem_config(
            {
                "siem_config_schema_version": 1,
                "transport": "syslog_tls",
            }
        )


def test_should_export_defense_in_depth() -> None:
    cfg = parse_siem_config(
        {
            "siem_config_schema_version": 1,
            "filters": {
                "min_severity": "high",
                "families": [
                    "queue_overflow",
                    "backup_coverage_failed",
                    "integrity_check_failed",
                    "restore_verification_failed",
                    "data_gap",
                    "agent_authentication_failed",
                ],
            },
        }
    )
    assert cfg.should_export(SiemEventFamily.AGENT_OFFLINE, SiemSeverity.MEDIUM) is False
    assert cfg.should_export(SiemEventFamily.QUEUE_OVERFLOW, SiemSeverity.CRITICAL) is True
    assert cfg.should_export(SiemEventFamily.BACKUP_COVERAGE_FAILED, SiemSeverity.HIGH) is True


def test_safe_dict_redacts_secret_ref() -> None:
    cfg = parse_siem_config(
        {
            "siem_config_schema_version": 1,
            "enabled": True,
            "endpoint": "https://siem.example/ingest",
            "auth": {
                "type": "bearer",
                "token": {"source": "env", "name": "BACKUPLINT_SIEM_TOKEN"},
            },
        }
    )
    safe = cfg.to_safe_dict()
    assert safe["auth_token"]["name"] == "BACKUPLINT_SIEM_TOKEN"
    assert "BACKUPLINT_SIEM_TOKEN" not in str(safe.get("auth_token", {})).replace(
        safe["auth_token"]["name"], ""
    )
