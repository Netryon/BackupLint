"""Classify discovered mounts as persistent, cache, temporary, or unknown."""

from __future__ import annotations

from enum import StrEnum

from backuplint.models import Mount, MountType


class StorageClass(StrEnum):
    PERSISTENT = "persistent"
    CACHE = "cache"
    TEMPORARY = "temporary"
    INFRASTRUCTURE = "infrastructure"
    UNKNOWN = "unknown"


# Conservative, explicit rules. Prefer unknown over silent ignore.
# These are container mount target patterns, not host temp-file usage.
_CACHE_TARGET_HINTS = (
    "/cache",
    "/.cache",
    "/var/cache",
    "/tmp/cache",  # nosec B108
)
_TEMPORARY_TARGET_HINTS = (
    "/tmp",  # nosec B108
    "/var/tmp",  # nosec B108
    "/dev/shm",  # nosec B108
    "/run",
)

# Obvious non-data OS/runtime binds. Not a blanket /etc/* exclusion.
_INFRA_EXACT = frozenset(
    {
        "/etc/localtime",
        "/etc/timezone",
        "/var/run/docker.sock",
        "/run/docker.sock",
    }
)
_INFRA_RO_SOURCE_PREFIXES = (
    "/etc/ssl/certs",
    "/etc/pki",
    "/usr/share/ca-certificates",
    "/usr/share/zoneinfo",
)
_INFRA_RO_TARGET_PREFIXES = (
    "/etc/ssl/certs",
    "/etc/pki",
    "/usr/share/ca-certificates",
    "/usr/share/zoneinfo",
)


def _norm(path: str) -> str:
    text = (path or "").strip()
    if not text:
        return ""
    return text.rstrip("/") or "/"


def _is_infrastructure_bind(mount: Mount) -> bool:
    """True only for explicit OS/runtime binds, never named volumes."""
    if mount.type is not MountType.BIND:
        return False
    source = _norm(mount.source).lower()
    target = _norm(mount.target).lower()
    if source in _INFRA_EXACT or target in _INFRA_EXACT:
        return True
    if not mount.read_only:
        return False
    source_hit = any(
        source == prefix or source.startswith(prefix + "/")
        for prefix in _INFRA_RO_SOURCE_PREFIXES
    )
    target_hit = any(
        target == prefix or target.startswith(prefix + "/")
        for prefix in _INFRA_RO_TARGET_PREFIXES
    )
    return source_hit and target_hit


def classify_mount(mount: Mount) -> StorageClass:
    """Classify a single mount using explicit, understandable rules."""
    if mount.type is MountType.TMPFS:
        return StorageClass.TEMPORARY

    if _is_infrastructure_bind(mount):
        return StorageClass.INFRASTRUCTURE

    target = mount.target.rstrip("/") or "/"
    target_lower = target.lower()

    for hint in _CACHE_TARGET_HINTS:
        if target_lower == hint or target_lower.endswith(hint):
            return StorageClass.CACHE

    for hint in _TEMPORARY_TARGET_HINTS:
        if target_lower == hint or target_lower.startswith(hint + "/"):
            # /tmp/cache already handled above as cache.
            return StorageClass.TEMPORARY

    if mount.type in {MountType.BIND, MountType.VOLUME}:
        # Config/data-style targets and generic bind/volume mounts are treated
        # as persistent unless matched by a temporary/cache/infrastructure rule.
        return StorageClass.PERSISTENT

    return StorageClass.UNKNOWN
