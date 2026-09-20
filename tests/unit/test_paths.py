"""Unit tests for path normalization and coverage matching."""

from __future__ import annotations

from pathlib import Path

import pytest

from backuplint.coverage import CoverageStatus, evaluate_mount
from backuplint.models import Mount, MountType
from backuplint.paths import (
    PathResolutionError,
    find_covering_backup_path,
    is_path_covered,
    normalize_path,
)


def test_exact_path_match(tmp_path: Path) -> None:
    path = tmp_path / "app"
    path.mkdir()
    assert is_path_covered(path, path) is True


def test_parent_directory_covers_child(tmp_path: Path) -> None:
    parent = tmp_path / "docker"
    child = parent / "sonarr"
    child.mkdir(parents=True)
    assert is_path_covered(child, parent) is True


def test_child_backup_does_not_cover_parent(tmp_path: Path) -> None:
    parent = tmp_path / "docker"
    child = parent / "sonarr"
    child.mkdir(parents=True)
    assert is_path_covered(parent, child) is False


def test_similar_name_is_not_covered() -> None:
    assert is_path_covered("/srv/app2", "/srv/app") is False
    assert is_path_covered("/srv/app", "/srv/app2") is False


def test_trailing_slashes_normalized() -> None:
    assert is_path_covered("/srv/app/", "/srv/app") is True
    assert is_path_covered("/srv/app", "/srv/app/") is True


def test_duplicate_slashes_normalized() -> None:
    assert is_path_covered("/srv//app", "/srv/app") is True


def test_dot_dot_normalized() -> None:
    assert is_path_covered("/srv/apps/../apps/app", "/srv/apps/app") is True
    assert is_path_covered("/srv/apps/app", "/srv/apps/../apps") is True


def test_relative_and_absolute_equivalent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.chdir(tmp_path)
    assert is_path_covered("./data", data) is True
    assert is_path_covered("data", data.resolve()) is True


def test_paths_with_spaces(tmp_path: Path) -> None:
    path = tmp_path / "app data"
    path.mkdir()
    assert is_path_covered(path, tmp_path) is True
    assert is_path_covered(path, tmp_path / "app") is False


def test_find_covering_backup_path_prefers_first_match() -> None:
    covered = find_covering_backup_path(
        "/srv/docker/sonarr",
        ["/srv/other", "/srv/docker", "/srv"],
    )
    assert covered == normalize_path("/srv/docker")


def test_missing_backup_returns_none() -> None:
    assert find_covering_backup_path("/srv/vaultwarden", ["/srv/docker"]) is None


def test_nonexistent_paths_can_still_match_lexically() -> None:
    assert is_path_covered("/srv/new-app/config", "/srv/new-app") is True


def test_symlink_coverage_uses_resolved_path(tmp_path: Path) -> None:
    real = tmp_path / "real-data"
    real.mkdir()
    link = tmp_path / "link-data"
    link.symlink_to(real)
    backup = tmp_path / "real-data"
    assert is_path_covered(link, backup) is True


def test_broken_symlink_raises(tmp_path: Path) -> None:
    link = tmp_path / "broken"
    link.symlink_to(tmp_path / "missing-target")
    with pytest.raises(PathResolutionError, match="Broken symlink"):
        normalize_path(link)


def test_unreadable_path_falls_back_to_lexical_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "restricted" / "data"

    def boom(self: Path) -> bool:  # noqa: ARG001
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "is_symlink", boom)
    normalized = normalize_path(target)
    assert normalized == Path(str(target)).absolute()
    assert is_path_covered(target, tmp_path / "restricted") is True


def test_broken_symlink_mount_is_warning_not_pass() -> None:
    mount = Mount(
        service="app",
        type=MountType.BIND,
        source="/tmp/definitely-broken-backuplint-link",
        target="/config",
    )
    # Simulate broken symlink by patching normalize via a real broken link path.
    # Direct unit behavior is covered by evaluate_mount catching PathResolutionError.
    from unittest.mock import patch

    with patch(
        "backuplint.coverage.normalize_path",
        side_effect=PathResolutionError("Broken symlink cannot be resolved safely: x"),
    ):
        finding = evaluate_mount(mount, ("/srv",))
    assert finding.status is CoverageStatus.UNSUPPORTED
    assert "Broken symlink" in finding.detail
