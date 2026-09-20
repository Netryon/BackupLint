"""Resolve Docker named volume host paths for coverage checks."""

from __future__ import annotations

import re
import shutil

from backuplint.compose import ComposeError
from backuplint.process import TimeoutExpired, run_argv

# Docker volume names are constrained; reject anything unexpected before exec.
_VOLUME_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


def resolve_volume_mountpoint(volume_name: str) -> str | None:
    """Return the host mountpoint for an existing Docker volume, if available."""
    if not _VOLUME_NAME_RE.fullmatch(volume_name):
        raise ComposeError(f"Refusing to inspect invalid Docker volume name: {volume_name!r}")

    docker = shutil.which("docker")
    if docker is None:
        raise ComposeError("Docker is not installed or not available on PATH.")

    command = [
        docker,
        "volume",
        "inspect",
        volume_name,
        "--format",
        "{{.Mountpoint}}",
    ]
    try:
        completed = run_argv(command, timeout=30)
    except TimeoutExpired as exc:
        raise ComposeError("Docker volume inspection timed out.") from exc
    except OSError as exc:
        raise ComposeError(f"Failed to inspect Docker volume: {exc}") from exc

    if completed.returncode != 0:
        return None

    mountpoint = completed.stdout.strip()
    return mountpoint or None
