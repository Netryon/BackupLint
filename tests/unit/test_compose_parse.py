"""Unit tests for Compose config parsing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backuplint.compose import ComposeError, format_mount_report, parse_compose_config
from backuplint.models import MountType

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "compose"


def test_parse_bind_volume_tmpfs_and_empty_service() -> None:
    data = json.loads((FIXTURES / "parsed-multi.json").read_text())
    services = {item.name: item for item in parse_compose_config(data)}

    assert set(services) == {"empty", "postgres", "sonarr"}
    assert services["empty"].mounts == ()

    sonarr = services["sonarr"].mounts
    assert len(sonarr) == 1
    assert sonarr[0].type is MountType.BIND
    assert sonarr[0].source == "/srv/sonarr"
    assert sonarr[0].target == "/config"
    assert sonarr[0].read_only is False

    postgres = services["postgres"].mounts
    assert [m.type for m in postgres] == [
        MountType.VOLUME,
        MountType.TMPFS,
        MountType.BIND,
    ]
    assert postgres[0].source == "postgres-data"
    assert postgres[2].read_only is True


def test_parse_string_volume_fallback() -> None:
    data = json.loads((FIXTURES / "parsed-string-volume.json").read_text())
    services = parse_compose_config(data)
    assert len(services) == 1
    mount = services[0].mounts[0]
    assert mount.type is MountType.BIND
    assert mount.source == "./data"
    assert mount.target == "/data"
    assert mount.read_only is True


# Add explicit regression for falsy non-dict services values.
def test_parse_services_list_is_rejected() -> None:
    with pytest.raises(ComposeError, match="invalid services"):
        parse_compose_config({"services": []})


def test_parse_missing_services_defaults_empty() -> None:
    assert parse_compose_config({}) == []


def test_format_mount_report() -> None:
    data = json.loads((FIXTURES / "parsed-multi.json").read_text())
    report = format_mount_report(parse_compose_config(data))
    assert "sonarr" in report
    assert "bind: /srv/sonarr:/config  [persistent]" in report
    assert "volume: postgres-data:/var/lib/postgresql/data  [persistent]" in report
    assert "tmpfs: /tmp  [temporary]" in report
    assert "empty" in report
    assert "(no mounts)" in report
