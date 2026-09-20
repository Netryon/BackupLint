"""Regression: restored broken symlinks must not fail validation."""

from __future__ import annotations

from pathlib import Path

from backuplint.restore_verify import RestoreVerificationStatus, validate_restored_paths


def test_validate_accepts_broken_symlink_beside_readable_file(tmp_path: Path) -> None:
    root = tmp_path / "restore"
    data = root / "srv" / "data"
    data.mkdir(parents=True)
    (data / "ok.txt").write_text("ok\n", encoding="utf-8")
    (data / "broken").symlink_to(data / "missing-target")
    result = validate_restored_paths(
        restore_root=root,
        source_paths=("/srv/data",),
    )
    assert result.status is RestoreVerificationStatus.PASSED
    assert result.files_checked >= 2
