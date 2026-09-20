"""Engine-neutral types for backup-engine adapters (v1: Restic only)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class OperationalKind(StrEnum):
    """Generic BackupLint operational failure kinds (engine-agnostic)."""

    REPOSITORY_UNAVAILABLE = "repository_unavailable"
    AUTHENTICATION = "authentication"
    LOCKED = "locked"
    NETWORK = "network"
    PERMISSION = "permission"
    CORRUPTION = "corruption"
    SNAPSHOT_NOT_FOUND = "snapshot_not_found"
    UNSUPPORTED = "unsupported"
    RUNTIME = "runtime"


@dataclass(frozen=True)
class EngineCapabilities:
    """What a backup engine can do in this BackupLint build."""

    engine_id: str
    list_snapshots: bool = True
    standard_integrity: bool = True
    deep_integrity: bool = False
    isolated_restore: bool = False
    version_reporting: bool = True


@dataclass(frozen=True)
class RepositoryStatus:
    """Basic repository availability / metadata probe result."""

    available: bool
    engine_id: str
    message: str
    kind: OperationalKind | None = None
    engine_version: str | None = None
