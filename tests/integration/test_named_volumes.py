"""Real Docker tests for named volume coverage behavior."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from backuplint.compose import discover_mounts
from backuplint.coverage import CoverageStatus, evaluate_coverage
from backuplint.models import MountType

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="Docker is required for named volume integration tests",
)


def _compose_up(compose: Path) -> None:
    subprocess.run(
        ["docker", "compose", "-f", str(compose), "up", "-d", "--quiet-pull"],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(compose.parent),
    )


def _compose_down(compose: Path) -> None:
    subprocess.run(
        ["docker", "compose", "-f", str(compose), "down", "-v", "--remove-orphans"],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(compose.parent),
    )


def test_one_named_volume_protected_and_unprotected(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n"
        "  app:\n"
        "    image: alpine:3.20\n"
        "    command: ['sleep', '30']\n"
        "    volumes:\n"
        "      - data:/data\n"
        "volumes:\n"
        "  data:\n"
    )
    try:
        _compose_up(compose)
        services = discover_mounts(compose)
        mounts = [m for s in services for m in s.mounts]
        assert len(mounts) == 1
        assert mounts[0].type is MountType.VOLUME

        covered = evaluate_coverage(services, ("/var/lib/docker/volumes",))
        missing = evaluate_coverage(services, (str(tmp_path / "backups"),))
        assert covered[0].status is CoverageStatus.PROTECTED
        assert missing[0].status is CoverageStatus.NOT_PROTECTED
        assert missing[0].host_path and missing[0].host_path.endswith("/_data")
    finally:
        _compose_down(compose)


def test_multiple_named_volumes(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n"
        "  app:\n"
        "    image: alpine:3.20\n"
        "    command: ['sleep', '30']\n"
        "    volumes:\n"
        "      - one:/one\n"
        "      - two:/two\n"
        "volumes:\n"
        "  one:\n"
        "  two:\n"
    )
    try:
        _compose_up(compose)
        findings = evaluate_coverage(discover_mounts(compose), ("/var/lib/docker/volumes",))
        assert len(findings) == 2
        assert all(f.status is CoverageStatus.PROTECTED for f in findings)
    finally:
        _compose_down(compose)


def test_read_only_named_volume(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n"
        "  app:\n"
        "    image: alpine:3.20\n"
        "    command: ['sleep', '30']\n"
        "    volumes:\n"
        "      - data:/data:ro\n"
        "volumes:\n"
        "  data:\n"
    )
    try:
        _compose_up(compose)
        mount = discover_mounts(compose)[0].mounts[0]
        assert mount.read_only is True
        finding = evaluate_coverage(discover_mounts(compose), ("/var/lib/docker/volumes",))[0]
        assert finding.status is CoverageStatus.PROTECTED
    finally:
        _compose_down(compose)


def test_shared_named_volume_across_services(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n"
        "  a:\n"
        "    image: alpine:3.20\n"
        "    command: ['sleep', '30']\n"
        "    volumes:\n"
        "      - shared:/data\n"
        "  b:\n"
        "    image: alpine:3.20\n"
        "    command: ['sleep', '30']\n"
        "    volumes:\n"
        "      - shared:/data\n"
        "volumes:\n"
        "  shared:\n"
    )
    try:
        _compose_up(compose)
        findings = evaluate_coverage(discover_mounts(compose), ("/var/lib/docker/volumes",))
        assert len(findings) == 2
        assert {f.service for f in findings} == {"a", "b"}
        assert len({f.host_path for f in findings}) == 1
        assert all(f.status is CoverageStatus.PROTECTED for f in findings)
    finally:
        _compose_down(compose)


def test_external_named_volume(tmp_path: Path) -> None:
    volume_name = f"backuplint_ext_{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "volume", "create", volume_name], check=True, capture_output=True)
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n"
        "  app:\n"
        "    image: alpine:3.20\n"
        "    command: ['sleep', '30']\n"
        "    volumes:\n"
        "      - data:/data\n"
        "volumes:\n"
        "  data:\n"
        "    external: true\n"
        f"    name: {volume_name}\n"
    )
    try:
        _compose_up(compose)
        mount = discover_mounts(compose)[0].mounts[0]
        assert mount.source == volume_name
        finding = evaluate_coverage(discover_mounts(compose), ("/var/lib/docker/volumes",))[0]
        assert finding.status is CoverageStatus.PROTECTED
        assert volume_name in (finding.host_path or "")
    finally:
        _compose_down(compose)
        subprocess.run(["docker", "volume", "rm", volume_name], check=False, capture_output=True)


def test_unused_declared_volume_is_not_reported(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n"
        "  app:\n"
        "    image: alpine:3.20\n"
        "    command: ['sleep', '30']\n"
        "    volumes:\n"
        "      - used:/data\n"
        "volumes:\n"
        "  used:\n"
        "  unused:\n"
    )
    try:
        _compose_up(compose)
        mounts = [m for s in discover_mounts(compose) for m in s.mounts]
        assert len(mounts) == 1
        assert mounts[0].source.endswith("_used")
        assert not mounts[0].source.endswith("_unused")
    finally:
        _compose_down(compose)
