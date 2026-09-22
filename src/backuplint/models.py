"""Data models for BackupLint discovery and reporting."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MountType(StrEnum):
    BIND = "bind"
    VOLUME = "volume"
    TMPFS = "tmpfs"
    NPIPE = "npipe"
    IMAGE = "image"
    CLUSTER = "cluster"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Mount:
    """A single mount discovered from Compose configuration."""

    service: str
    type: MountType
    source: str
    target: str
    read_only: bool = False

    def summary_line(self) -> str:
        mode = " (ro)" if self.read_only else ""
        if self.type is MountType.BIND:
            return f"  bind: {self.source}:{self.target}{mode}"
        if self.type is MountType.VOLUME:
            return f"  volume: {self.source}:{self.target}{mode}"
        if self.type is MountType.TMPFS:
            return f"  tmpfs: {self.target}{mode}"
        return f"  {self.type.value}: {self.source}:{self.target}{mode}"


@dataclass(frozen=True)
class ServiceMounts:
    name: str
    mounts: tuple[Mount, ...]
    image: str | None = None
    project: str | None = None
