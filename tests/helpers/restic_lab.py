"""Shared disposable Restic lab helpers for integration tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def restic_env_with_password(password: str) -> dict[str, str]:
    """Build an env for restic using RESTIC_PASSWORD; clear conflicting ambient vars."""
    env = os.environ.copy()
    env.pop("RESTIC_PASSWORD_FILE", None)
    env.pop("RESTIC_REPOSITORY", None)
    env["RESTIC_PASSWORD"] = password
    return env


def restic_env_with_password_file(
    password_file: Path, *, repository: Path | None = None
) -> dict[str, str]:
    """Build an env for restic using RESTIC_PASSWORD_FILE."""
    env = os.environ.copy()
    env.pop("RESTIC_PASSWORD", None)
    env["RESTIC_PASSWORD_FILE"] = str(password_file)
    if repository is not None:
        env["RESTIC_REPOSITORY"] = str(repository)
    else:
        env.pop("RESTIC_REPOSITORY", None)
    return env


def init_repo(repo: Path, password: str) -> None:
    env = restic_env_with_password(password)
    subprocess.run(
        ["restic", "-r", str(repo), "init"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def backup_paths(repo: Path, password: str, *paths: Path) -> None:
    env = restic_env_with_password(password)
    cmd = ["restic", "-r", str(repo), "backup", *[str(path) for path in paths]]
    subprocess.run(cmd, check=True, capture_output=True, text=True, env=env)


def init_and_backup_with_password_file(
    repo: Path, password_file: Path, *paths: Path, password: str = "lab-pass\n"  # noqa: S107
) -> None:
    password_file.write_text(password, encoding="utf-8")
    password_file.chmod(0o600)
    env = restic_env_with_password_file(password_file, repository=repo)
    subprocess.run(["restic", "init"], check=True, env=env, capture_output=True)
    subprocess.run(
        ["restic", "backup", *[str(path) for path in paths]],
        check=True,
        env=env,
        capture_output=True,
    )
