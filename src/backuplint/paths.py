"""Path normalization and backup coverage matching."""

from __future__ import annotations

import os
from pathlib import Path


class PathResolutionError(Exception):
    """Raised when a path cannot be normalized safely."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def normalize_path(path: str | Path) -> Path:
    """Normalize a path for coverage comparison.

    Resolves ``.`` / ``..``, expands ``~``, collapses redundant separators, and
    follows symlinks when the target exists. Broken symlinks raise
    ``PathResolutionError`` so callers can warn instead of silently passing.

    Paths that exist but cannot be inspected (for example Docker volume
    directories without privilege) fall back to lexical normalization so coverage
    matching still works.
    """
    candidate = Path(path).expanduser()
    try:
        if candidate.is_symlink() and not candidate.exists():
            raise PathResolutionError(
                f"Broken symlink cannot be resolved safely: {candidate}"
            )
        if candidate.exists():
            return candidate.resolve()
    except OSError:
        pass
    absolute = candidate if candidate.is_absolute() else Path.cwd() / candidate
    # Lexical normalization for paths that are not present on disk yet,
    # or that cannot be inspected due to permissions.
    return Path(os.path.normpath(str(absolute)))


def is_path_covered(data_path: str | Path, backup_path: str | Path) -> bool:
    """Return True if ``backup_path`` exactly matches or is a parent of ``data_path``.

    Uses pathlib parent/child semantics so ``/srv/app`` does not cover ``/srv/app2``.
    """
    data = normalize_path(data_path)
    backup = normalize_path(backup_path)
    if data == backup:
        return True
    try:
        data.relative_to(backup)
    except ValueError:
        return False
    return True


def find_covering_backup_path(
    data_path: str | Path,
    backup_paths: list[str] | tuple[str, ...],
) -> Path | None:
    """Return the first configured backup path that covers ``data_path``, if any.

    Backup paths that cannot be resolved safely are skipped.
    """
    for backup in backup_paths:
        try:
            if is_path_covered(data_path, backup):
                return normalize_path(backup)
        except PathResolutionError:
            continue
    return None
