"""CLI smoke tests for the minimal BackupLint interface."""

from __future__ import annotations

import re

from typer.testing import CliRunner

from backuplint import __version__
from backuplint.cli import app

runner = CliRunner()


def _plain(text: str) -> str:
    """Strip ANSI styling so assertions work under CI forced-color terminals."""
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def test_help_succeeds() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    output = _plain(result.stdout)
    assert "BackupLint" in output or "backuplint" in output
    assert "--help" in output
    assert "--version" in output


def test_version_succeeds() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout
    assert "backuplint" in result.stdout


def test_version_short_flag() -> None:
    result = runner.invoke(app, ["-V"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "--help" in _plain(result.stdout)


def test_unknown_argument_fails_cleanly() -> None:
    result = runner.invoke(app, ["--not-a-real-flag"])
    assert result.exit_code != 0
    # Typer/Click should report the bad option without a traceback for users.
    combined = _plain((result.stdout or "") + (result.stderr or ""))
    assert "No such option" in combined or "no such option" in combined.lower()
    assert "Traceback" not in combined
