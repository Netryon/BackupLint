"""File permission / type checks for secrets and TLS private keys."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from backuplint.secrets.errors import (
    SecretMissingError,
    SecretPermissionError,
    SecretResolutionError,
    SecretTooLargeError,
)

# Conservative default; Restic passwords and tokens are small.
DEFAULT_MAX_SECRET_BYTES = 64 * 1024


@dataclass(frozen=True)
class SensitiveFileReport:
    path: str
    exists: bool
    is_regular_file: bool | None
    is_symlink: bool | None
    mode: str | None
    world_readable: bool | None
    world_writable: bool | None
    issues: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.issues and self.exists and bool(self.is_regular_file)


def diagnose_sensitive_file(path: Path) -> SensitiveFileReport:
    """Inspect a sensitive key/cert file without reading its contents."""
    path = Path(path)
    issues: list[str] = []
    if not path.exists():
        return SensitiveFileReport(
            path=str(path),
            exists=False,
            is_regular_file=None,
            is_symlink=None,
            mode=None,
            world_readable=None,
            world_writable=None,
            issues=("file does not exist",),
        )
    try:
        st = path.lstat()
    except OSError as exc:
        return SensitiveFileReport(
            path=str(path),
            exists=True,
            is_regular_file=None,
            is_symlink=None,
            mode=None,
            world_readable=None,
            world_writable=None,
            issues=(f"stat failed: {exc}",),
        )
    is_symlink = stat.S_ISLNK(st.st_mode)
    mode = oct(st.st_mode & 0o777)
    if is_symlink:
        try:
            st = path.stat()
        except OSError:
            issues.append("broken symlink")
            return SensitiveFileReport(
                path=str(path),
                exists=True,
                is_regular_file=False,
                is_symlink=True,
                mode=mode,
                world_readable=None,
                world_writable=None,
                issues=tuple(issues),
            )
    is_reg = stat.S_ISREG(st.st_mode)
    if not is_reg:
        issues.append("not a regular file")
    world_readable = bool(st.st_mode & stat.S_IROTH)
    world_writable = bool(st.st_mode & stat.S_IWOTH)
    if world_writable:
        issues.append("world-writable")
    if world_readable:
        issues.append("world-readable")
    if st.st_mode & stat.S_IRGRP and not world_readable:
        # Group-readable private keys are often undesirable on multi-user hosts.
        issues.append("group-readable")
    return SensitiveFileReport(
        path=str(path),
        exists=True,
        is_regular_file=is_reg,
        is_symlink=is_symlink,
        mode=oct(st.st_mode & 0o777),
        world_readable=world_readable,
        world_writable=world_writable,
        issues=tuple(issues),
    )


def read_secret_file(
    path: Path,
    *,
    max_bytes: int = DEFAULT_MAX_SECRET_BYTES,
    allow_symlink: bool = True,
    require_owner_only: bool = True,
    strip_single_trailing_newline: bool = True,
) -> str:
    """Read a secret file with type/permission/size guards.

    Trailing newline: if ``strip_single_trailing_newline`` is true, exactly one
    trailing ``\\n`` is removed (common for files created with ``echo``). Other
    whitespace is preserved.
    """
    path = Path(path).expanduser()
    try:
        lst = path.lstat()
    except FileNotFoundError as exc:
        raise SecretMissingError(f"secret file not found: {path}") from exc
    except OSError as exc:
        raise SecretResolutionError("unable to stat secret file") from exc

    if stat.S_ISLNK(lst.st_mode):
        if not allow_symlink:
            raise SecretPermissionError(f"secret path is a symlink (rejected): {path}")
        if not path.exists():
            raise SecretMissingError(f"secret file is a broken symlink: {path}")
        try:
            st = path.stat()
        except OSError as exc:
            raise SecretResolutionError("unable to follow secret symlink") from exc
    else:
        st = lst

    if stat.S_ISDIR(st.st_mode):
        raise SecretPermissionError(f"secret path is a directory: {path}")
    if stat.S_ISFIFO(st.st_mode) or stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode):
        raise SecretPermissionError(f"secret path is not a regular file: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise SecretPermissionError(f"secret path is not a regular file: {path}")

    if st.st_mode & stat.S_IWOTH:
        raise SecretPermissionError(f"secret file is world-writable: {path}")
    if require_owner_only:
        if st.st_mode & stat.S_IROTH:
            raise SecretPermissionError(f"secret file is world-readable: {path}")
        if st.st_mode & (stat.S_IRGRP | stat.S_IWGRP):
            raise SecretPermissionError(
                f"secret file is group-accessible (use mode 0600): {path}"
            )

    if st.st_size > max_bytes:
        raise SecretTooLargeError(
            f"secret file exceeds maximum size ({max_bytes} bytes)"
        )

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SecretResolutionError("unable to read secret file") from exc
    if len(raw) > max_bytes:
        raise SecretTooLargeError(
            f"secret file exceeds maximum size ({max_bytes} bytes)"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretResolutionError("secret file is not valid UTF-8") from exc

    if strip_single_trailing_newline and text.endswith("\n"):
        text = text[:-1]
    if text.endswith("\r"):
        # CRLF files: strip CR left after newline handling.
        text = text[:-1]
    if not text:
        raise SecretMissingError("secret file is empty")
    return text


def credentials_directory() -> Path | None:
    """Return systemd CREDENTIALS_DIRECTORY if set and usable."""
    raw = os.environ.get("CREDENTIALS_DIRECTORY")
    if raw is None or not raw.strip():
        return None
    path = Path(raw)
    if not path.is_dir():
        return None
    return path
