"""Safe argv-list subprocess helpers for external tools."""

from __future__ import annotations

import subprocess  # nosec B404 — argv-list invocations only
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def run_argv(
    command: list[str],
    *,
    timeout: float,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> CommandResult:
    """Run an external command without a shell.

    Callers must pass a validated argv list. This helper never uses ``shell=True``.
    """
    completed = subprocess.run(  # noqa: S603  # nosec B603
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=cwd,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


# Re-export timeout exception so callers need not import subprocess directly.
TimeoutExpired = subprocess.TimeoutExpired
