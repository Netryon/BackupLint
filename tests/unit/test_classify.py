"""Unit tests for mount storage classification."""

from __future__ import annotations

from backuplint.classify import StorageClass, classify_mount
from backuplint.models import Mount, MountType


def _mount(
    *,
    mtype: MountType = MountType.BIND,
    source: str = "/srv/data",
    target: str = "/data",
    read_only: bool = False,
) -> Mount:
    return Mount(
        service="app",
        type=mtype,
        source=source,
        target=target,
        read_only=read_only,
    )


def test_config_directory_is_persistent() -> None:
    assert classify_mount(_mount(target="/config")) is StorageClass.PERSISTENT


def test_named_volume_is_persistent() -> None:
    mount = _mount(mtype=MountType.VOLUME, source="pgdata", target="/var/lib/postgresql/data")
    assert classify_mount(mount) is StorageClass.PERSISTENT


def test_cache_directory() -> None:
    assert classify_mount(_mount(target="/cache")) is StorageClass.CACHE
    assert classify_mount(_mount(target="/var/cache")) is StorageClass.CACHE
    assert classify_mount(_mount(target="/tmp/cache")) is StorageClass.CACHE


def test_temporary_paths() -> None:
    assert classify_mount(_mount(target="/tmp")) is StorageClass.TEMPORARY
    assert classify_mount(_mount(target="/var/tmp/work")) is StorageClass.TEMPORARY
    assert classify_mount(_mount(target="/run/lock")) is StorageClass.TEMPORARY


def test_tmpfs_is_temporary() -> None:
    mount = _mount(mtype=MountType.TMPFS, source="", target="/scratch")
    assert classify_mount(mount) is StorageClass.TEMPORARY


def test_unknown_mount_type_stays_unknown() -> None:
    mount = _mount(mtype=MountType.NPIPE, source="//./pipe", target="/pipe")
    assert classify_mount(mount) is StorageClass.UNKNOWN


def test_uncertain_targets_default_persistent_for_bind() -> None:
    # Prefer not silently ignoring uncertain bind mounts.
    assert classify_mount(_mount(target="/opt/custom")) is StorageClass.PERSISTENT
