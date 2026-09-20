"""Compare discovered mounts with configured backup paths or Restic snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from backuplint.classify import StorageClass, classify_mount
from backuplint.compose import ComposeError
from backuplint.database import database_warning_message, is_database_image
from backuplint.models import Mount, MountType, ServiceMounts
from backuplint.paths import (
    PathResolutionError,
    find_covering_backup_path,
    normalize_path,
)
from backuplint.restic import ResticSnapshot, latest_relevant_snapshot
from backuplint.timeutil import format_age
from backuplint.volumes import resolve_volume_mountpoint


class CoverageStatus(StrEnum):
    PROTECTED = "protected"
    NOT_PROTECTED = "not protected"
    SKIPPED = "skipped"
    UNSUPPORTED = "unsupported"
    STALE = "stale"


@dataclass(frozen=True)
class CoverageFinding:
    service: str
    mount: Mount | None
    storage_class: StorageClass | None
    status: CoverageStatus
    detail: str
    host_path: str | None = None
    covered_by: str | None = None
    snapshot_time: datetime | None = None

    @property
    def is_critical(self) -> bool:
        return self.status is CoverageStatus.NOT_PROTECTED


def _resolve_host_path(
    mount: Mount,
    *,
    volume_resolver=None,
) -> tuple[str | None, CoverageFinding | None]:
    """Return (host_path, early_finding). early_finding is set for skip/unsupported."""
    if volume_resolver is None:
        volume_resolver = resolve_volume_mountpoint
    storage_class = classify_mount(mount)

    if storage_class in {StorageClass.CACHE, StorageClass.TEMPORARY}:
        return None, CoverageFinding(
            service=mount.service,
            mount=mount,
            storage_class=storage_class,
            status=CoverageStatus.SKIPPED,
            detail=f"{storage_class.value} storage is not required to be backed up",
        )

    if mount.type is MountType.VOLUME:
        try:
            mountpoint = volume_resolver(mount.source)
        except ComposeError:
            return None, CoverageFinding(
                service=mount.service,
                mount=mount,
                storage_class=storage_class,
                status=CoverageStatus.UNSUPPORTED,
                detail="unable to resolve named volume host path",
                host_path=mount.source,
            )
        if not mountpoint:
            return None, CoverageFinding(
                service=mount.service,
                mount=mount,
                storage_class=storage_class,
                status=CoverageStatus.UNSUPPORTED,
                detail="named volume not found on this Docker host",
                host_path=mount.source,
            )
        return str(normalize_path(mountpoint)), None

    if mount.type is not MountType.BIND:
        return None, CoverageFinding(
            service=mount.service,
            mount=mount,
            storage_class=storage_class,
            status=CoverageStatus.UNSUPPORTED,
            detail=f"{mount.type.value} mounts are not checked against backup_paths",
            host_path=mount.source or None,
        )

    try:
        return str(normalize_path(mount.source)), None
    except PathResolutionError as exc:
        return None, CoverageFinding(
            service=mount.service,
            mount=mount,
            storage_class=storage_class,
            status=CoverageStatus.UNSUPPORTED,
            detail=exc.message,
            host_path=mount.source,
        )


def evaluate_mount(
    mount: Mount,
    backup_paths: tuple[str, ...],
    *,
    covered_detail: str = "covered by configured backup path",
    missing_detail: str = "not covered by configured backup_paths",
    volume_resolver=None,
) -> CoverageFinding:
    """Evaluate a single mount against configured backup paths."""
    host_path, early = _resolve_host_path(mount, volume_resolver=volume_resolver)
    if early is not None:
        return early
    if host_path is None:
        return CoverageFinding(
            service=mount.service,
            mount=mount,
            storage_class=classify_mount(mount),
            status=CoverageStatus.UNSUPPORTED,
            detail="unable to resolve host path",
        )
    storage_class = classify_mount(mount)

    covered_by = find_covering_backup_path(host_path, backup_paths)
    if covered_by is not None:
        return CoverageFinding(
            service=mount.service,
            mount=mount,
            storage_class=storage_class,
            status=CoverageStatus.PROTECTED,
            detail=covered_detail,
            host_path=host_path,
            covered_by=str(covered_by),
        )

    return CoverageFinding(
        service=mount.service,
        mount=mount,
        storage_class=storage_class,
        status=CoverageStatus.NOT_PROTECTED,
        detail=missing_detail,
        host_path=host_path,
    )


def evaluate_mount_restic(
    mount: Mount,
    snapshots: list[ResticSnapshot],
    *,
    max_backup_age: timedelta | None = None,
    now: datetime | None = None,
    volume_resolver=None,
) -> CoverageFinding:
    """Evaluate a mount against the newest Restic snapshot that covers its path."""
    host_path, early = _resolve_host_path(mount, volume_resolver=volume_resolver)
    if early is not None:
        return early
    if host_path is None:
        return CoverageFinding(
            service=mount.service,
            mount=mount,
            storage_class=classify_mount(mount),
            status=CoverageStatus.UNSUPPORTED,
            detail="unable to resolve host path",
        )
    storage_class = classify_mount(mount)

    if not snapshots:
        return CoverageFinding(
            service=mount.service,
            mount=mount,
            storage_class=storage_class,
            status=CoverageStatus.NOT_PROTECTED,
            detail="no Restic snapshots found",
            host_path=host_path,
        )

    relevant = latest_relevant_snapshot(snapshots, host_path)
    if relevant is None:
        return CoverageFinding(
            service=mount.service,
            mount=mount,
            storage_class=storage_class,
            status=CoverageStatus.NOT_PROTECTED,
            detail="no Restic snapshot covers this path",
            host_path=host_path,
        )

    covered_root = find_covering_backup_path(host_path, relevant.paths)
    covered_by = str(covered_root) if covered_root is not None else None
    current = now or datetime.now(UTC)

    if relevant.time is None:
        if max_backup_age is not None:
            return CoverageFinding(
                service=mount.service,
                mount=mount,
                storage_class=storage_class,
                status=CoverageStatus.STALE,
                detail=(
                    f"Restic snapshot {relevant.short_id} covers this path but has "
                    "no usable timestamp"
                ),
                host_path=host_path,
                covered_by=covered_by,
            )
        return CoverageFinding(
            service=mount.service,
            mount=mount,
            storage_class=storage_class,
            status=CoverageStatus.PROTECTED,
            detail=f"covered by Restic snapshot {relevant.short_id}",
            host_path=host_path,
            covered_by=covered_by,
        )

    snapshot_time = relevant.time
    if snapshot_time.tzinfo is None:
        snapshot_time = snapshot_time.replace(tzinfo=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)

    age = current - snapshot_time
    age_text = format_age(age)

    if max_backup_age is not None and age > max_backup_age:
        return CoverageFinding(
            service=mount.service,
            mount=mount,
            storage_class=storage_class,
            status=CoverageStatus.STALE,
            detail=(
                f"latest relevant backup {age_text} ago "
                f"(snapshot {relevant.short_id}, max {format_age(max_backup_age)})"
            ),
            host_path=host_path,
            covered_by=covered_by,
            snapshot_time=snapshot_time,
        )

    return CoverageFinding(
        service=mount.service,
        mount=mount,
        storage_class=storage_class,
        status=CoverageStatus.PROTECTED,
        detail=f"backed up {age_text} ago (snapshot {relevant.short_id})",
        host_path=host_path,
        covered_by=covered_by,
        snapshot_time=snapshot_time,
    )


def evaluate_coverage(
    services: list[ServiceMounts],
    backup_paths: tuple[str, ...],
    *,
    covered_detail: str = "covered by configured backup path",
    missing_detail: str = "not covered by configured backup_paths",
) -> list[CoverageFinding]:
    """Evaluate all mounts from discovered services and database warnings."""
    findings: list[CoverageFinding] = []
    for service in services:
        for mount in service.mounts:
            findings.append(
                evaluate_mount(
                    mount,
                    backup_paths,
                    covered_detail=covered_detail,
                    missing_detail=missing_detail,
                )
            )
        if is_database_image(service.image):
            findings.append(
                CoverageFinding(
                    service=service.name,
                    mount=None,
                    storage_class=None,
                    status=CoverageStatus.UNSUPPORTED,
                    detail=database_warning_message(service.image or ""),
                    host_path="database detected",
                )
            )
    return findings


def evaluate_coverage_restic(
    services: list[ServiceMounts],
    snapshots: list[ResticSnapshot],
    *,
    max_backup_age: timedelta | None = None,
    now: datetime | None = None,
) -> list[CoverageFinding]:
    """Evaluate mounts using per-path relevant Restic snapshots."""
    findings: list[CoverageFinding] = []
    for service in services:
        for mount in service.mounts:
            findings.append(
                evaluate_mount_restic(
                    mount,
                    snapshots,
                    max_backup_age=max_backup_age,
                    now=now,
                )
            )
        if is_database_image(service.image):
            findings.append(
                CoverageFinding(
                    service=service.name,
                    mount=None,
                    storage_class=None,
                    status=CoverageStatus.UNSUPPORTED,
                    detail=database_warning_message(service.image or ""),
                    host_path="database detected",
                )
            )
    return findings

