"""Isolated restore destination creation, ownership, and safe cleanup."""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

SENTINEL_NAME = ".backuplint-restore-sentinel"


class RestoreDestinationError(Exception):
    """Unsafe or unusable restore destination."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class OwnedRestoreRoot:
    """A temporary restore root created and owned by BackupLint for one run."""

    path: Path
    run_id: str
    created_by_backuplint: bool = True

    @property
    def sentinel_path(self) -> Path:
        return self.path / SENTINEL_NAME


def _resolve(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


_FORBIDDEN_EXACT = frozenset(
    {
        Path("/"),
        Path("/home"),
        Path("/var"),
        Path("/tmp"),  # noqa: S108  # nosec B108 — forbidden destination, not a temp file
        Path("/var/lib/docker"),
        Path("/root"),
        Path("/etc"),
        Path("/usr"),
        Path("/boot"),
        Path("/opt"),
        Path("/bin"),
        Path("/sbin"),
        Path("/lib"),
        Path("/lib64"),
        Path("/dev"),
        Path("/proc"),
        Path("/sys"),
        Path("/run"),
    }
)

# Parents that must never host restore temp trees (hostile TMPDIR=/etc etc.).
# Note: /home is NOT listed — user ~/.cache/backuplint is an allowed fallback.
# /tmp and /var/tmp remain allowed parents (exact /tmp and /var are still forbidden destinations).
_FORBIDDEN_PARENT_PREFIXES = (
    Path("/etc"),
    Path("/usr"),
    Path("/boot"),
    Path("/bin"),
    Path("/sbin"),
    Path("/lib"),
    Path("/lib64"),
    Path("/opt"),
    Path("/root"),
    Path("/dev"),
    Path("/proc"),
    Path("/sys"),
    Path("/run"),
)


def _is_under_forbidden_parent(path: Path) -> bool:
    resolved = _resolve(path)
    for prefix in _FORBIDDEN_PARENT_PREFIXES:
        if resolved == prefix:
            return True
        try:
            resolved.relative_to(prefix)
            return True
        except ValueError:
            continue
    return False


def assert_safe_restore_parent(parent: Path) -> Path:
    """Reject hostile TMPDIR / restore parents under sensitive system prefixes."""
    resolved = _resolve(parent)
    if resolved in _FORBIDDEN_EXACT or _is_under_forbidden_parent(resolved):
        raise RestoreDestinationError(
            f"Restore temp parent {resolved} is not allowed "
            "(refusing system/sensitive paths and hostile TMPDIR)."
        )
    return resolved


def assert_safe_restore_target(
    candidate: Path,
    *,
    repository: str | None = None,
    live_bind_paths: tuple[Path, ...] = (),
    live_volume_mountpoints: tuple[Path, ...] = (),
) -> Path:
    """Reject destinations that could overwrite live or system data."""
    resolved = _resolve(candidate)

    if resolved in _FORBIDDEN_EXACT or _is_under_forbidden_parent(resolved):
        raise RestoreDestinationError(
            f"Restore destination {resolved} is not allowed."
        )

    # Refuse home directory roots like /home/user
    if resolved.parent == Path("/home") and resolved.name:
        raise RestoreDestinationError(
            f"Restore destination {resolved} must not be a home directory root."
        )

    if repository:
        # Only apply filesystem comparisons for local-looking repositories.
        if "://" not in repository and not repository.startswith("sftp:"):
            try:
                repo_resolved = _resolve(Path(repository))
            except OSError:
                repo_resolved = None
            if repo_resolved is not None:
                if resolved == repo_resolved or resolved == repo_resolved.parent:
                    raise RestoreDestinationError(
                        "Restore destination must not be the Restic repository "
                        "or its parent directory."
                    )
                try:
                    resolved.relative_to(repo_resolved)
                    raise RestoreDestinationError(
                        "Restore destination must not be inside the Restic repository."
                    )
                except ValueError:
                    pass

    for live in (*live_bind_paths, *live_volume_mountpoints):
        live_resolved = _resolve(live)
        if resolved == live_resolved:
            raise RestoreDestinationError(
                "Restore destination must not be a live audited data path."
            )
        try:
            resolved.relative_to(live_resolved)
            raise RestoreDestinationError(
                "Restore destination must not be inside a live audited data path."
            )
        except ValueError:
            pass

    return resolved


def create_owned_restore_root(
    *,
    parent: Path | None = None,
    repository: str | None = None,
    live_bind_paths: tuple[Path, ...] = (),
    live_volume_mountpoints: tuple[Path, ...] = (),
) -> OwnedRestoreRoot:
    """Create a unique owned restore directory with a sentinel file."""
    run_id = secrets.token_hex(16)
    if parent is not None:
        base_parent = assert_safe_restore_parent(parent)
    else:
        # Prefer XDG/runtime state over raw TMPDIR when TMPDIR is hostile.
        env_tmp = Path(tempfile.gettempdir())
        try:
            base_parent = assert_safe_restore_parent(env_tmp)
        except RestoreDestinationError:
            fallback = Path.home() / ".cache" / "backuplint" / "restore-tmp"
            fallback.mkdir(parents=True, exist_ok=True)
            os.chmod(fallback, 0o700)
            base_parent = assert_safe_restore_parent(fallback)
    if not base_parent.is_dir():
        raise RestoreDestinationError(
            f"Restore temp parent does not exist: {base_parent}"
        )

    path = base_parent / f"backuplint-restore-{run_id}"
    if path.exists():
        raise RestoreDestinationError(
            f"Restore destination already exists (collision): {path}"
        )

    assert_safe_restore_target(
        path,
        repository=repository,
        live_bind_paths=live_bind_paths,
        live_volume_mountpoints=live_volume_mountpoints,
    )

    path.mkdir(mode=0o700, exist_ok=False)
    os.chmod(path, 0o700)
    sentinel = path / SENTINEL_NAME
    sentinel.write_text(f"{run_id}\n", encoding="utf-8")
    os.chmod(sentinel, 0o600)
    return OwnedRestoreRoot(path=path, run_id=run_id)


def _ownership_ok(root: OwnedRestoreRoot) -> None:
    if not root.created_by_backuplint:
        raise RestoreDestinationError(
            "Refusing to clean a restore destination not created by BackupLint."
        )
    if not root.path.is_dir():
        raise RestoreDestinationError(
            f"Restore destination missing or not a directory: {root.path}"
        )
    if root.path.is_symlink():
        raise RestoreDestinationError(
            "Restore destination became a symlink; refusing cleanup."
        )
    if not root.sentinel_path.is_file():
        raise RestoreDestinationError(
            "Restore destination sentinel missing; refusing cleanup."
        )
    try:
        content = root.sentinel_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RestoreDestinationError(
            "Unable to read restore destination sentinel; refusing cleanup."
        ) from exc
    if content != root.run_id:
        raise RestoreDestinationError(
            "Restore destination sentinel mismatch; refusing cleanup."
        )


def cleanup_owned_restore_root(root: OwnedRestoreRoot) -> None:
    """Delete an owned restore root after validating the sentinel."""
    _ownership_ok(root)
    shutil.rmtree(root.path)


def expected_materialized_path(restore_root: Path, source_path: str | Path) -> Path:
    """Map an absolute backup path to its location under a restore target."""
    source = Path(source_path)
    parts = source.parts
    if parts and parts[0] == "/":
        rel = Path(*parts[1:]) if len(parts) > 1 else Path()
    else:
        rel = source
    return restore_root / rel
