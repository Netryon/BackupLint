"""Regression tests for per-path relevant Restic snapshot selection and freshness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from backuplint.coverage import CoverageStatus, evaluate_mount_restic
from backuplint.models import Mount, MountType
from backuplint.restic import ResticSnapshot, latest_relevant_snapshot, latest_snapshot


def _snap(
    short_id: str,
    when: datetime | None,
    *paths: str,
) -> ResticSnapshot:
    return ResticSnapshot(
        snapshot_id=short_id * 4,
        short_id=short_id,
        time=when,
        paths=paths,
    )


def test_global_latest_is_unrelated_but_relevant_snapshot_is_used() -> None:
    docker = _snap(
        "dock0001",
        datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        "/srv/docker",
    )
    home = _snap(
        "home0001",
        datetime(2026, 1, 1, 10, 30, tzinfo=UTC),
        "/home/user",
    )
    etc = _snap(
        "etc00001",
        datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
        "/etc",
    )
    snapshots = [docker, home, etc]

    assert latest_snapshot(snapshots) is etc
    relevant = latest_relevant_snapshot(snapshots, "/srv/docker/sonarr")
    assert relevant is docker

    mount = Mount(
        service="sonarr",
        type=MountType.BIND,
        source="/srv/docker/sonarr",
        target="/config",
    )
    finding = evaluate_mount_restic(
        mount,
        snapshots,
        now=datetime(2026, 1, 1, 11, 30, tzinfo=UTC),
    )
    assert finding.status is CoverageStatus.PROTECTED
    assert finding.covered_by == "/srv/docker"
    assert "dock0001" in finding.detail


def test_no_relevant_snapshot_is_not_protected() -> None:
    etc = _snap(
        "etc00001",
        datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
        "/etc",
    )
    mount = Mount(
        service="sonarr",
        type=MountType.BIND,
        source="/srv/docker/sonarr",
        target="/config",
    )
    finding = evaluate_mount_restic(mount, [etc])
    assert finding.status is CoverageStatus.NOT_PROTECTED
    assert "no Restic snapshot covers this path" in finding.detail


def test_freshness_exactly_at_threshold_is_protected() -> None:
    now = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    snap = _snap("fresh001", now - timedelta(hours=24), "/srv/docker")
    mount = Mount(
        service="app",
        type=MountType.BIND,
        source="/srv/docker/app",
        target="/data",
    )
    finding = evaluate_mount_restic(
        mount,
        [snap],
        max_backup_age=timedelta(hours=24),
        now=now,
    )
    assert finding.status is CoverageStatus.PROTECTED
    assert "backed up 1d ago" in finding.detail


def test_freshness_just_before_threshold_is_protected() -> None:
    now = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    snap = _snap("fresh001", now - timedelta(hours=24) + timedelta(seconds=1), "/srv/docker")
    mount = Mount(
        service="app",
        type=MountType.BIND,
        source="/srv/docker/app",
        target="/data",
    )
    finding = evaluate_mount_restic(
        mount,
        [snap],
        max_backup_age=timedelta(hours=24),
        now=now,
    )
    assert finding.status is CoverageStatus.PROTECTED


def test_freshness_just_after_threshold_is_stale() -> None:
    now = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    snap = _snap("stale001", now - timedelta(hours=24) - timedelta(seconds=1), "/srv/docker")
    mount = Mount(
        service="app",
        type=MountType.BIND,
        source="/srv/docker/app",
        target="/data",
    )
    finding = evaluate_mount_restic(
        mount,
        [snap],
        max_backup_age=timedelta(hours=24),
        now=now,
    )
    assert finding.status is CoverageStatus.STALE
    assert "latest relevant backup" in finding.detail


def test_missing_timestamp_with_max_age_is_stale_warning() -> None:
    snap = _snap("notime001", None, "/srv/docker")
    mount = Mount(
        service="app",
        type=MountType.BIND,
        source="/srv/docker/app",
        target="/data",
    )
    finding = evaluate_mount_restic(
        mount,
        [snap],
        max_backup_age=timedelta(hours=24),
    )
    assert finding.status is CoverageStatus.STALE
    assert "no usable timestamp" in finding.detail


def test_naive_snapshot_time_treated_as_utc() -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    snap = _snap("naive001", datetime(2026, 1, 1, 10, 0), "/srv/docker")
    mount = Mount(
        service="app",
        type=MountType.BIND,
        source="/srv/docker/app",
        target="/data",
    )
    finding = evaluate_mount_restic(
        mount,
        [snap],
        max_backup_age=timedelta(hours=3),
        now=now,
    )
    assert finding.status is CoverageStatus.PROTECTED
    assert finding.snapshot_time is not None
    assert finding.snapshot_time.tzinfo is not None
