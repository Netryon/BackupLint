"""Unit tests for restore destination safety and ownership."""

from __future__ import annotations

from pathlib import Path

import pytest

from backuplint.restore_dest import (
    RestoreDestinationError,
    assert_safe_restore_target,
    cleanup_owned_restore_root,
    create_owned_restore_root,
    expected_materialized_path,
)


def test_expected_materialized_path_joins_absolute_under_target(tmp_path: Path) -> None:
    out = expected_materialized_path(tmp_path, "/srv/app/data")
    assert out == tmp_path / "srv" / "app" / "data"


def test_rejects_root_and_home(tmp_path: Path) -> None:
    with pytest.raises(RestoreDestinationError):
        assert_safe_restore_target(Path("/"))
    with pytest.raises(RestoreDestinationError):
        assert_safe_restore_target(Path("/home/someone"))


def test_rejects_repository_and_live_bind(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    live = tmp_path / "live-data"
    live.mkdir()
    with pytest.raises(RestoreDestinationError):
        assert_safe_restore_target(repo, repository=str(repo))
    with pytest.raises(RestoreDestinationError):
        assert_safe_restore_target(live, live_bind_paths=(live,))


def test_similar_prefix_live_path_not_confused(tmp_path: Path) -> None:
    app = tmp_path / "data" / "app"
    app2 = tmp_path / "data" / "app2"
    app.mkdir(parents=True)
    app2.mkdir(parents=True)
    # Destination under app2 must not be rejected solely because app is live.
    assert_safe_restore_target(app2 / "restore", live_bind_paths=(app,))


def test_owned_root_create_cleanup_roundtrip(tmp_path: Path) -> None:
    root = create_owned_restore_root(parent=tmp_path)
    assert root.path.is_dir()
    assert root.sentinel_path.read_text(encoding="utf-8").strip() == root.run_id
    cleanup_owned_restore_root(root)
    assert not root.path.exists()


def test_cleanup_refuses_missing_or_tampered_sentinel(tmp_path: Path) -> None:
    root = create_owned_restore_root(parent=tmp_path)
    root.sentinel_path.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(RestoreDestinationError):
        cleanup_owned_restore_root(root)
    # restore sentinel for cleanup of leftover dir in fixture teardown
    root.sentinel_path.write_text(f"{root.run_id}\n", encoding="utf-8")
    cleanup_owned_restore_root(root)
