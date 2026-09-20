"""Crash-safe local locking for the BackupLint scheduler daemon."""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]


class ScheduleLockError(Exception):
    """Unable to acquire or manage the scheduler lock."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class ScheduleLock:
    """Exclusive flock-backed lock with owner metadata."""

    path: Path
    _fd: int | None = None

    def acquire(self, *, blocking: bool = False) -> None:
        if fcntl is None:
            raise ScheduleLockError("Scheduler locking requires fcntl (POSIX).")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Restrictive permissions before writing secrets-adjacent metadata.
        fd = os.open(
            self.path,
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError as exc:
            os.close(fd)
            raise ScheduleLockError(
                f"Scheduler lock is held by another process: {self.path}"
            ) from exc
        except OSError as exc:
            os.close(fd)
            raise ScheduleLockError(f"Unable to acquire scheduler lock: {exc}") from exc

        payload = {
            "pid": os.getpid(),
            "started_at": time.time(),
            "hostname": socket.gethostname(),
        }
        try:
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps(payload, indent=2).encode("utf-8"))
            os.fsync(fd)
        except OSError as exc:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise ScheduleLockError(f"Unable to write scheduler lock metadata: {exc}") from exc
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> ScheduleLock:
        self.acquire()
        return self

    def __exit__(self, *args: object) -> None:
        self.release()

    def read_owner(self) -> dict[str, object] | None:
        if not self.path.is_file():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None
