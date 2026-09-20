"""Adversarial false-PASS hunts for production readiness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from backuplint.coverage import CoverageStatus, evaluate_mount, evaluate_mount_restic
from backuplint.models import Mount, MountType
from backuplint.restic import ResticSnapshot


def test_missing_named_volume_is_not_protected() -> None:
    """Unresolved volumes must never look like successful protection."""
    volume = Mount(
        service="db",
        type=MountType.VOLUME,
        source="missing_vol",
        target="/var/lib/postgresql/data",
    )
    with patch("backuplint.coverage.resolve_volume_mountpoint", return_value=None):
        finding = evaluate_mount(volume, ("/var/lib/docker/volumes",))
    assert finding.status is not CoverageStatus.PROTECTED
    assert finding.status is CoverageStatus.UNSUPPORTED


def test_cache_under_data_name_still_skipped_documented() -> None:
    """/cache targets are skipped — document heuristic risk for unusual layouts."""
    mount = Mount(
        service="app",
        type=MountType.BIND,
        source="/srv/app/cache",
        target="/cache",
    )
    finding = evaluate_mount(mount, ())
    assert finding.status is CoverageStatus.SKIPPED


def test_restic_missing_timestamp_without_max_age_is_protected_documented() -> None:
    """Known limitation: covering snapshot without time is PROTECTED if no max_backup_age."""
    mount = Mount(
        service="app",
        type=MountType.BIND,
        source="/srv/app",
        target="/data",
    )
    snap = ResticSnapshot(
        snapshot_id="abc",
        short_id="abc",
        time=None,
        paths=("/srv/app",),
    )
    finding = evaluate_mount_restic(mount, [snap], max_backup_age=None)
    assert finding.status is CoverageStatus.PROTECTED


def test_restic_missing_timestamp_with_max_age_is_stale() -> None:
    mount = Mount(
        service="app",
        type=MountType.BIND,
        source="/srv/app",
        target="/data",
    )
    snap = ResticSnapshot(
        snapshot_id="abc",
        short_id="abc",
        time=None,
        paths=("/srv/app",),
    )
    finding = evaluate_mount_restic(
        mount,
        [snap],
        max_backup_age=timedelta(hours=24),
    )
    assert finding.status is CoverageStatus.STALE


def test_unrelated_newer_snapshot_does_not_false_pass() -> None:
    mount = Mount(
        service="app",
        type=MountType.BIND,
        source="/srv/docker/app",
        target="/data",
    )
    older = ResticSnapshot(
        snapshot_id="old",
        short_id="old",
        time=datetime(2026, 1, 1, tzinfo=UTC),
        paths=("/srv/docker",),
    )
    newer = ResticSnapshot(
        snapshot_id="new",
        short_id="new",
        time=datetime(2026, 1, 2, tzinfo=UTC),
        paths=("/etc",),
    )
    finding = evaluate_mount_restic(
        mount,
        [older, newer],
        max_backup_age=timedelta(days=3650),
        now=datetime(2026, 1, 2, 12, tzinfo=UTC),
    )
    assert finding.status is CoverageStatus.PROTECTED
    assert finding.covered_by is not None
    assert finding.detail.startswith("backed up")
