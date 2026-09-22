"""Unit tests for coverage evaluation."""

from __future__ import annotations

from unittest.mock import patch

from backuplint.coverage import CoverageStatus, evaluate_mount
from backuplint.models import Mount, MountType
from backuplint.reporting import format_coverage_report


def _bind(service: str, source: str, target: str = "/config") -> Mount:
    return Mount(service=service, type=MountType.BIND, source=source, target=target)


def test_exact_and_parent_coverage() -> None:
    exact = evaluate_mount(_bind("sonarr", "/srv/docker/sonarr"), ("/srv/docker/sonarr",))
    parent = evaluate_mount(_bind("radarr", "/srv/docker/radarr"), ("/srv/docker",))
    assert exact.status is CoverageStatus.PROTECTED
    assert parent.status is CoverageStatus.PROTECTED
    assert parent.covered_by is not None


def test_missing_path_not_protected() -> None:
    finding = evaluate_mount(_bind("vaultwarden", "/srv/vaultwarden"), ("/srv/docker",))
    assert finding.status is CoverageStatus.NOT_PROTECTED
    assert finding.is_critical is True


def test_similar_name_not_protected() -> None:
    finding = evaluate_mount(_bind("app2", "/srv/app2"), ("/srv/app",))
    assert finding.status is CoverageStatus.NOT_PROTECTED


def test_blank_backup_list_marks_persistent_unprotected() -> None:
    finding = evaluate_mount(_bind("sonarr", "/srv/docker/sonarr"), ())
    assert finding.status is CoverageStatus.NOT_PROTECTED


def test_cache_and_tmpfs_skipped() -> None:
    cache = Mount(
        service="app",
        type=MountType.BIND,
        source="/srv/cache",
        target="/cache",
    )
    tmpfs = Mount(
        service="app",
        type=MountType.TMPFS,
        source="",
        target="/tmp/work",
    )
    assert evaluate_mount(cache, ("/srv",)).status is CoverageStatus.SKIPPED
    assert evaluate_mount(tmpfs, ("/srv",)).status is CoverageStatus.SKIPPED


def test_named_volume_coverage_when_mountpoint_known() -> None:
    volume = Mount(
        service="db",
        type=MountType.VOLUME,
        source="project_pgdata",
        target="/var/lib/postgresql/data",
    )
    # Use a synthetic mountpoint path that will not collide with a real,
    # permission-restricted Docker root on CI runners.
    mountpoint = "/var/tmp/backuplint-fake-docker/volumes/project_pgdata/_data"
    with patch(
        "backuplint.coverage.resolve_volume_mountpoint",
        return_value=mountpoint,
    ):
        protected = evaluate_mount(
            volume,
            ("/var/tmp/backuplint-fake-docker/volumes",),
        )
        missing = evaluate_mount(volume, ("/srv/docker",))
    assert protected.status is CoverageStatus.PROTECTED
    assert missing.status is CoverageStatus.NOT_PROTECTED


def test_localtime_uncovered_is_skipped_not_fail() -> None:
    finding = evaluate_mount(
        _bind("homeassistant", "/etc/localtime", "/etc/localtime"),
        ("/opt/hass/config",),
    )
    assert finding.status is CoverageStatus.SKIPPED
    assert finding.storage_class.value == "infrastructure"


def test_localtime_explicitly_listed_is_protected() -> None:
    finding = evaluate_mount(
        _bind("homeassistant", "/etc/localtime", "/etc/localtime"),
        ("/etc/localtime",),
    )
    assert finding.status is CoverageStatus.PROTECTED


def test_writable_config_bind_still_required() -> None:
    finding = evaluate_mount(
        _bind("homeassistant", "/opt/hass/config", "/config"),
        ("/srv/unrelated",),
    )
    assert finding.status is CoverageStatus.NOT_PROTECTED
    assert finding.is_critical is True


def test_named_volume_unsupported_when_missing() -> None:
    volume = Mount(
        service="db",
        type=MountType.VOLUME,
        source="pgdata",
        target="/var/lib/postgresql/data",
    )
    with patch("backuplint.coverage.resolve_volume_mountpoint", return_value=None):
        finding = evaluate_mount(volume, ("/var/lib/docker/volumes",))
    assert finding.status is CoverageStatus.UNSUPPORTED


def test_format_coverage_report_includes_summary() -> None:
    findings = [
        evaluate_mount(_bind("sonarr", "/srv/docker/sonarr"), ("/srv/docker",)),
        evaluate_mount(_bind("vault", "/srv/vault"), ("/srv/docker",)),
    ]
    report = format_coverage_report(findings)
    assert "BackupLint Audit" in report
    assert "Result: FAIL" in report
    assert "protected" in report
    assert "critical" in report
    assert "sonarr" in report
    assert "vault" in report
