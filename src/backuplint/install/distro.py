"""Distro family detection for native installer adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class DistroFamily(StrEnum):
    DEBIAN = "debian"  # Debian/Ubuntu → apt
    RHEL = "rhel"  # Fedora/Rocky/RHEL → dnf
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class DistroInfo:
    family: DistroFamily
    id: str
    version_id: str | None
    pretty_name: str | None
    detail: str

    @property
    def supported(self) -> bool:
        return self.family in {DistroFamily.DEBIAN, DistroFamily.RHEL}


def _parse_os_release(text: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        data[key.strip()] = value
    return data


def detect_distro(*, os_release_path: Path | None = None) -> DistroInfo:
    """Detect supported native Linux family. Never mutates the host."""
    path = os_release_path or Path("/etc/os-release")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return DistroInfo(
            family=DistroFamily.UNSUPPORTED,
            id="unknown",
            version_id=None,
            pretty_name=None,
            detail=f"unable to read {path}: {exc}",
        )
    fields = _parse_os_release(text)
    distro_id = (fields.get("ID") or "").lower()
    id_like = (fields.get("ID_LIKE") or "").lower().split()
    version_id = fields.get("VERSION_ID")
    pretty = fields.get("PRETTY_NAME")

    debianish = {"debian", "ubuntu", "linuxmint", "pop", "raspbian"}
    rhelish = {"rhel", "fedora", "centos", "rocky", "almalinux", "ol"}

    if distro_id in debianish or "debian" in id_like or "ubuntu" in id_like:
        return DistroInfo(
            family=DistroFamily.DEBIAN,
            id=distro_id or "debian",
            version_id=version_id,
            pretty_name=pretty,
            detail="apt-based family detected",
        )
    if distro_id in rhelish or any(token in id_like for token in rhelish):
        return DistroInfo(
            family=DistroFamily.RHEL,
            id=distro_id or "rhel",
            version_id=version_id,
            pretty_name=pretty,
            detail="dnf-based family detected",
        )
    return DistroInfo(
        family=DistroFamily.UNSUPPORTED,
        id=distro_id or "unknown",
        version_id=version_id,
        pretty_name=pretty,
        detail=f"unsupported distro id={distro_id!r} id_like={id_like!r}",
    )
