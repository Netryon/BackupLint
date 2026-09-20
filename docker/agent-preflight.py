#!/usr/bin/env python3
"""Preflight checks for agent container state directory permissions."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: agent-preflight.py STATE_DIR", file=sys.stderr)
        return 2
    state_dir = Path(argv[1])
    uid = os.getuid()
    gid = os.getgid()
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "identity").mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(
            f"ERROR: cannot create state directory {state_dir} "
            f"(uid={uid} gid={gid}): {exc}. "
            "Mount a volume at /state owned by UID/GID 10001 "
            "(or matching BACKUPLINT_UID/GID). "
            "Do not run privileged/root to bypass ownership.",
            file=sys.stderr,
        )
        return 1
    if not os.access(state_dir, os.W_OK | os.X_OK):
        print(
            f"ERROR: state directory {state_dir} is not writable by uid={uid} gid={gid}. "
            "Fix volume ownership (chown 10001:10001). Privileged mode must not be used.",
            file=sys.stderr,
        )
        return 1
    try:
        fd, probe = tempfile.mkstemp(prefix=".backuplint-write-", dir=str(state_dir))
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        print(
            f"ERROR: failed write probe in {state_dir} (uid={uid}): {exc}.",
            file=sys.stderr,
        )
        return 1
    # Warn if private key permissions are too open.
    key = state_dir / "identity" / "client.key"
    if key.is_file():
        mode = key.stat().st_mode & 0o777
        if mode & 0o077:
            print(
                f"WARNING: {key} mode is {oct(mode)}; expected 0600.",
                file=sys.stderr,
            )
    # Docker socket must not be present by default.
    if Path("/var/run/docker.sock").exists():
        print(
            "WARNING: /var/run/docker.sock is present inside the container. "
            "BackupLint agent images do not require the Docker socket by default; "
            "remove it unless you intentionally mounted Compose tooling.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
