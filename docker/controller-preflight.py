#!/usr/bin/env python3
"""Preflight checks for controller container state directory permissions."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: controller-preflight.py DATA_DIR", file=sys.stderr)
        return 2
    data_dir = Path(argv[1])
    uid = os.getuid()
    gid = os.getgid()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(
            f"ERROR: cannot create state directory {data_dir} "
            f"(uid={uid} gid={gid}): {exc}. "
            "Mount a volume at /state owned by UID/GID 10001 "
            "(or matching BACKUPLINT_UID/GID build args). "
            "Do not run this image as privileged/root to bypass ownership.",
            file=sys.stderr,
        )
        return 1
    if not os.access(data_dir, os.W_OK | os.X_OK):
        print(
            f"ERROR: state directory {data_dir} is not writable by uid={uid} gid={gid}. "
            "Fix volume ownership (chown 10001:10001) or rebuild with matching "
            "BACKUPLINT_UID/BACKUPLINT_GID. Privileged mode is not required and "
            "must not be used.",
            file=sys.stderr,
        )
        return 1
    try:
        fd, probe = tempfile.mkstemp(prefix=".backuplint-write-", dir=str(data_dir))
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        print(
            f"ERROR: failed write probe in {data_dir} (uid={uid}): {exc}. "
            "SQLite/WAL and certificate material cannot be persisted.",
            file=sys.stderr,
        )
        return 1
    siem_dir = data_dir / "siem"
    try:
        siem_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(
            f"ERROR: cannot create SIEM state directory {siem_dir} (uid={uid}): {exc}.",
            file=sys.stderr,
        )
        return 1
    # Warn (non-fatal) if existing private keys are overly permissive.
    for rel in ("ca/ca.key", "server/server.key"):
        key = data_dir / rel
        if key.is_file():
            mode = key.stat().st_mode & 0o777
            if mode & 0o077:
                print(
                    f"WARNING: {key} mode is {oct(mode)}; expected 0600. "
                    "Refusing to chmod automatically so operator intent is preserved.",
                    file=sys.stderr,
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
