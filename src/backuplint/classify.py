"""Classify discovered mounts as persistent, cache, temporary, or unknown."""

from __future__ import annotations

from enum import StrEnum

from backuplint.models import Mount, MountType


class StorageClass(StrEnum):
    PERSISTENT = "persistent"
    CACHE = "cache"
    TEMPORARY = "temporary"
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


def classify_mount(mount: Mount) -> StorageClass:
    """Classify a single mount using explicit, understandable rules."""
    if mount.type is MountType.TMPFS:
        return StorageClass.TEMPORARY

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
        # as persistent unless matched by a temporary/cache rule.
        return StorageClass.PERSISTENT

    return StorageClass.UNKNOWN
