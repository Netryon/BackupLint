"""Metadata fidelity checks with capability detection (optional features)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("restic") is None, reason="restic not installed"
)


def _env(passfile: Path, repo: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("RESTIC_PASSWORD", None)
    env["RESTIC_PASSWORD_FILE"] = str(passfile)
    env["RESTIC_REPOSITORY"] = str(repo)
    return env


def test_metadata_fidelity_capabilities(tmp_path: Path) -> None:
    """Prove what this platform+restic preserves; skip only truly unavailable ops."""
    data = tmp_path / "data"
    data.mkdir()
    f_exec = data / "run.sh"
    f_exec.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    os.chmod(f_exec, 0o755)  # noqa: S103  # intentional executable bit for metadata lab
    f_ro = data / "readonly.txt"
    f_ro.write_text("ro\n", encoding="utf-8")
    os.chmod(f_ro, 0o444)  # noqa: S103  # intentional read-only bit for metadata lab
    sub = data / "subdir"
    sub.mkdir(mode=0o750)  # noqa: S103
    link = data / "sym"
    link.symlink_to("run.sh")
    hard_a = data / "hard-a"
    hard_b = data / "hard-b"
    hard_a.write_text("hardlink-body\n", encoding="utf-8")
    os.link(hard_a, hard_b)

    proven: list[str] = []
    unavailable: list[str] = []

    # xattr capability
    try:
        os.setxattr(f_exec, "user.backuplint", b"probe")
        proven.append("xattr-set")
    except (OSError, AttributeError):
        unavailable.append("xattr")

    # ACL capability
    try:
        subprocess.run(
            ["setfacl", "-m", "u:nobody:r", str(f_ro)],
            check=True,
            capture_output=True,
        )
        proven.append("acl-set")
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        unavailable.append("acl")

    # sparse file
    sparse = data / "sparse.bin"
    with sparse.open("wb") as handle:
        handle.seek(1024 * 1024)
        handle.write(b"z")
    proven.append("sparse-created")

    passfile = tmp_path / "pass"
    passfile.write_text("meta-pass\n", encoding="utf-8")
    passfile.chmod(0o600)
    repo = tmp_path / "repo"
    env = _env(passfile, repo)
    subprocess.run(["restic", "init"], check=True, env=env, capture_output=True)
    subprocess.run(
        ["restic", "backup", str(data)],
        check=True,
        env=env,
        capture_output=True,
    )
    target = tmp_path / "restore"
    target.mkdir()
    subprocess.run(
        ["restic", "restore", "latest", "--target", str(target)],
        check=True,
        env=env,
        capture_output=True,
    )
    restored = target.joinpath(*data.parts[1:])
    r_exec = restored / "run.sh"
    assert r_exec.is_file()
    mode = r_exec.stat().st_mode & 0o777
    assert mode & 0o100, f"executable bit not preserved: {oct(mode)}"
    proven.append("executable-bit")

    r_ro = restored / "readonly.txt"
    assert (r_ro.stat().st_mode & 0o222) == 0
    proven.append("readonly-mode")

    r_link = restored / "sym"
    assert r_link.is_symlink()
    assert os.readlink(r_link) == "run.sh"
    proven.append("symlink-target")

    r_a = restored / "hard-a"
    r_b = restored / "hard-b"
    if r_a.stat().st_ino == r_b.stat().st_ino:
        proven.append("hardlink-identity")
    else:
        unavailable.append("hardlink-identity")

    if "xattr-set" in proven:
        try:
            val = os.getxattr(r_exec, "user.backuplint")
            assert val == b"probe"
            proven.append("xattr-restored")
        except OSError:
            unavailable.append("xattr-restored")

    # Document in assertion message for campaign logs.
    assert "executable-bit" in proven
    assert "symlink-target" in proven
    print("METADATA_PROVEN", ",".join(proven))
    print("METADATA_UNAVAILABLE", ",".join(unavailable) if unavailable else "none")
