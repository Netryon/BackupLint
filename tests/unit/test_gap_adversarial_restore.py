"""Expanded adversarial false-PASS hunts for restore verification."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from backuplint.config import RestoreVerificationMode
from backuplint.restic import ResticSnapshot
from backuplint.restore_verify import (
    RestoreVerificationStatus,
    validate_restored_paths,
    verify_restore,
)


def test_validate_missing_expected_relative_is_fail(tmp_path: Path) -> None:
    root = tmp_path / "restore"
    data = root / "srv" / "data"
    data.mkdir(parents=True)
    (data / "file.txt").write_text("x\n", encoding="utf-8")
    result = validate_restored_paths(
        restore_root=root,
        source_paths=("/srv/data",),
        expected_relative_paths=("must-exist.txt",),
    )
    assert result.status is RestoreVerificationStatus.FAILED


def test_validate_similar_name_only_is_fail(tmp_path: Path) -> None:
    root = tmp_path / "restore"
    # Similar sibling restored, expected path missing.
    sib = root / "srv" / "data2"
    sib.mkdir(parents=True)
    (sib / "file.txt").write_text("x\n", encoding="utf-8")
    result = validate_restored_paths(
        restore_root=root,
        source_paths=("/srv/data",),
    )
    assert result.status is RestoreVerificationStatus.FAILED


def test_stale_preexisting_files_do_not_count_without_expected_tree(
    tmp_path: Path,
) -> None:
    """Validation walks only materialized expected sources, not arbitrary leftovers."""
    root = tmp_path / "restore"
    junk = root / "unrelated"
    junk.mkdir(parents=True)
    (junk / "old.txt").write_text("stale\n", encoding="utf-8")
    result = validate_restored_paths(
        restore_root=root,
        source_paths=("/srv/data",),
    )
    assert result.status is RestoreVerificationStatus.FAILED
    assert result.files_checked == 0


def test_newest_unrelated_snapshot_does_not_satisfy_path() -> None:
    snaps = [
        ResticSnapshot(
            snapshot_id="old",
            short_id="old",
            time=datetime(2026, 1, 1, tzinfo=UTC),
            paths=("/srv/data",),
        ),
        ResticSnapshot(
            snapshot_id="new",
            short_id="new",
            time=datetime(2026, 2, 1, tzinfo=UTC),
            paths=("/etc",),
        ),
    ]
    with (
        patch(
            "backuplint.restore_verify.create_owned_restore_root"
        ) as create,
        patch(
            "backuplint.restore_verify.restore_snapshot_paths"
        ) as restore,
        patch(
            "backuplint.restore_verify.cleanup_owned_restore_root"
        ),
        patch(
            "backuplint.restore_verify.validate_restored_paths"
        ) as validate,
    ):
        owned = MagicMock()
        owned.path = Path("/tmp/backuplint-restore-test")
        create.return_value = owned
        restore.return_value = MagicMock(
            returncode=0,
            duration_seconds=0.1,
            sanitized_output="",
        )
        validate.return_value = MagicMock(
            status=RestoreVerificationStatus.PASSED,
            message="ok",
            files_checked=1,
            bytes_checked=1,
        )
        result = verify_restore(
            mode=RestoreVerificationMode.SELECTED,
            repository="/repo",
            snapshots=snaps,
            relevant_paths=("/srv/data",),
            password="x",
        )
    # Must select the covering snapshot (old), not newest unrelated.
    restore.assert_called()
    assert restore.call_args.kwargs["snapshot_id"] == "old"
    assert result.status is RestoreVerificationStatus.PASSED


def test_path_only_in_wrong_snapshot_fails_without_cover() -> None:
    snaps = [
        ResticSnapshot(
            snapshot_id="a",
            short_id="a",
            time=datetime(2026, 1, 1, tzinfo=UTC),
            paths=("/srv/other",),
        ),
    ]
    result = verify_restore(
        mode=RestoreVerificationMode.SELECTED,
        repository="/repo",
        snapshots=snaps,
        relevant_paths=("/srv/data",),
        password="x",
    )
    assert result.status is RestoreVerificationStatus.FAILED
    assert "no relevant snapshot" in result.message


def test_restic_exit_zero_but_missing_paths_is_fail(tmp_path: Path) -> None:
    snaps = [
        ResticSnapshot(
            snapshot_id="s1",
            short_id="s1",
            time=datetime(2026, 1, 1, tzinfo=UTC),
            paths=("/srv/data",),
        ),
    ]
    with (
        patch(
            "backuplint.restore_verify.create_owned_restore_root"
        ) as create,
        patch(
            "backuplint.restore_verify.restore_snapshot_paths"
        ) as restore,
        patch(
            "backuplint.restore_verify.cleanup_owned_restore_root"
        ),
    ):
        owned = MagicMock()
        owned.path = tmp_path / "owned"
        owned.path.mkdir()
        create.return_value = owned
        restore.return_value = MagicMock(
            returncode=0,
            duration_seconds=0.1,
            sanitized_output="",
        )
        result = verify_restore(
            mode=RestoreVerificationMode.SELECTED,
            repository="/repo",
            snapshots=snaps,
            relevant_paths=("/srv/data",),
            password="x",
            expected_paths=("critical.db",),
        )
    assert result.status is RestoreVerificationStatus.FAILED
