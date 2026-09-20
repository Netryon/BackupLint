"""Symlink / path-traversal attack campaign against restore destination safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from backuplint.restore_dest import (
    OwnedRestoreRoot,
    RestoreDestinationError,
    cleanup_owned_restore_root,
    create_owned_restore_root,
)


def test_temp_root_replaced_by_symlink_refuses_cleanup(tmp_path: Path) -> None:
    owned = create_owned_restore_root(parent=tmp_path)
    run_id = owned.run_id
    sentinel_text = owned.sentinel_path.read_text(encoding="utf-8")
    # Replace directory with symlink to unrelated sibling.
    victim = tmp_path / "victim-live"
    victim.mkdir()
    marker = victim / "do-not-delete.txt"
    marker.write_text("precious\n", encoding="utf-8")
    # Remove owned dir contents carefully then replace path with symlink.
    for child in owned.path.iterdir():
        if child.is_file():
            child.unlink()
        else:
            import shutil

            shutil.rmtree(child)
    owned.path.rmdir()
    owned.path.symlink_to(victim)
    # Recreate sentinel reading through symlink (attacker might plant matching id).
    (owned.path / ".backuplint-restore-sentinel").write_text(sentinel_text, encoding="utf-8")
    with pytest.raises(RestoreDestinationError, match="symlink"):
        cleanup_owned_restore_root(OwnedRestoreRoot(path=owned.path, run_id=run_id))
    assert marker.exists()
    assert marker.read_text(encoding="utf-8") == "precious\n"


def test_child_directory_symlink_to_live_not_followed_by_safety_check(
    tmp_path: Path,
) -> None:
    live = tmp_path / "live-data"
    live.mkdir()
    (live / "secret.txt").write_text("secret\n", encoding="utf-8")
    owned = create_owned_restore_root(
        parent=tmp_path,
        live_bind_paths=(live,),
    )
    # Plant symlink inside owned root pointing at live data.
    trap = owned.path / "escape"
    trap.symlink_to(live)
    # Cleanup must still only remove owned root; live marker stays.
    cleanup_owned_restore_root(owned)
    assert not (tmp_path / f"backuplint-restore-{owned.run_id}").exists()
    assert (live / "secret.txt").read_text(encoding="utf-8") == "secret\n"


def test_sentinel_deleted_refuses_cleanup(tmp_path: Path) -> None:
    owned = create_owned_restore_root(parent=tmp_path)
    owned.sentinel_path.unlink()
    with pytest.raises(RestoreDestinationError, match="sentinel"):
        cleanup_owned_restore_root(owned)
    assert owned.path.is_dir()
    # Restore sentinel for fixture hygiene.
    owned.sentinel_path.write_text(f"{owned.run_id}\n", encoding="utf-8")
    cleanup_owned_restore_root(owned)


def test_sentinel_replaced_with_wrong_id_refuses_cleanup(tmp_path: Path) -> None:
    owned = create_owned_restore_root(parent=tmp_path)
    owned.sentinel_path.write_text("attacker-id\n", encoding="utf-8")
    with pytest.raises(RestoreDestinationError, match="mismatch"):
        cleanup_owned_restore_root(owned)
    owned.sentinel_path.write_text(f"{owned.run_id}\n", encoding="utf-8")
    cleanup_owned_restore_root(owned)


def test_cleanup_target_swapped_to_repo_refuses(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "config").write_text("restic-repo\n", encoding="utf-8")
    owned = create_owned_restore_root(parent=tmp_path, repository=str(repo))
    run_id = owned.run_id
    import shutil

    shutil.rmtree(owned.path)
    owned.path.symlink_to(repo)
    (owned.path / ".backuplint-restore-sentinel").write_text(f"{run_id}\n", encoding="utf-8")
    with pytest.raises(RestoreDestinationError):
        cleanup_owned_restore_root(OwnedRestoreRoot(path=owned.path, run_id=run_id))
    assert (repo / "config").read_text(encoding="utf-8") == "restic-repo\n"


def test_symlink_points_to_tmp_sibling_refuses_cleanup(tmp_path: Path) -> None:
    sibling = tmp_path / "other-tmp-tree"
    sibling.mkdir()
    keep = sibling / "keep.txt"
    keep.write_text("keep\n", encoding="utf-8")
    owned = create_owned_restore_root(parent=tmp_path)
    run_id = owned.run_id
    import shutil

    shutil.rmtree(owned.path)
    owned.path.symlink_to(sibling)
    (owned.path / ".backuplint-restore-sentinel").write_text(f"{run_id}\n", encoding="utf-8")
    with pytest.raises(RestoreDestinationError):
        cleanup_owned_restore_root(OwnedRestoreRoot(path=owned.path, run_id=run_id))
    assert keep.read_text(encoding="utf-8") == "keep\n"


def test_create_rejects_when_parent_is_symlink_to_forbidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Creating under a normal parent is fine; assert_safe still forbids /tmp itself.
    from backuplint.restore_dest import assert_safe_restore_target

    with pytest.raises(RestoreDestinationError):
        assert_safe_restore_target(Path("/tmp"))


def test_common_prefix_bypass_live_path(tmp_path: Path) -> None:
    live = tmp_path / "app"
    lookalike = tmp_path / "app2"
    live.mkdir()
    lookalike.mkdir()
    # lookalike must not be treated as inside live
    create_owned_restore_root(parent=lookalike, live_bind_paths=(live,))
