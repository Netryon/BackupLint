"""Integration tests that invoke Docker Compose discovery."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from backuplint.compose import ComposeError, discover_mounts, format_mount_report
from backuplint.models import MountType

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "compose"

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="Docker is required for Compose integration tests",
)


def test_discover_one_service_bind(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text((FIXTURES / "one-service-bind.yml").read_text())
    (tmp_path / "sonarr").mkdir()

    services = discover_mounts(compose)
    assert len(services) == 1
    assert services[0].name == "sonarr"
    mount = services[0].mounts[0]
    assert mount.type is MountType.BIND
    assert mount.target == "/config"
    assert Path(mount.source) == (tmp_path / "sonarr").resolve()


def test_discover_multiple_services_bind_and_volume(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text((FIXTURES / "multi-service.yml").read_text())
    (tmp_path / "sonarr").mkdir()
    (tmp_path / "radarr").mkdir()

    services = {item.name: item for item in discover_mounts(compose)}
    assert set(services) == {"radarr", "sonarr"}
    assert any(m.type is MountType.BIND for m in services["sonarr"].mounts)
    assert any(
        m.type is MountType.VOLUME and "media" in m.source
        for m in services["radarr"].mounts
    )


def test_discover_no_volumes(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text((FIXTURES / "no-volumes.yml").read_text())
    services = discover_mounts(compose)
    assert len(services) == 1
    assert services[0].mounts == ()
    assert "(no mounts)" in format_mount_report(services)


def test_discover_read_only_bind(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text((FIXTURES / "read-only-bind.yml").read_text())
    (tmp_path / "config").mkdir()
    mount = discover_mounts(compose)[0].mounts[0]
    assert mount.read_only is True
    assert mount.type is MountType.BIND


def test_discover_absolute_bind(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text((FIXTURES / "absolute-bind.yml").read_text())
    mount = discover_mounts(compose)[0].mounts[0]
    assert mount.type is MountType.BIND
    assert mount.source == "/var/tmp/backuplint-abs-fixture"
    assert mount.target == "/data"


def test_discover_named_volume(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text((FIXTURES / "named-volume.yml").read_text())
    mount = discover_mounts(compose)[0].mounts[0]
    assert mount.type is MountType.VOLUME
    assert mount.target == "/var/lib/postgresql/data"
    # Compose resolves the concrete Docker volume name for the project.
    assert "pgdata" in mount.source


def test_missing_compose_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yml"
    with pytest.raises(ComposeError, match="not found"):
        discover_mounts(missing)


def test_invalid_compose_file(tmp_path: Path) -> None:
    compose = tmp_path / "invalid.yml"
    compose.write_text((FIXTURES / "invalid.yml").read_text())
    with pytest.raises(ComposeError, match="failed to parse"):
        discover_mounts(compose)
