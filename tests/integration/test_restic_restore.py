"""Integration tests for real Restic restore verification."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backuplint.cli import app

pytestmark = [
    pytest.mark.skipif(shutil.which("restic") is None, reason="restic not installed"),
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed"),
]

runner = CliRunner()


def _write_stack(base: Path, data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "payload.txt").write_text("restore-me\n", encoding="utf-8")
    compose = base / "compose.yml"
    compose.write_text(
        "services:\n"
        "  app:\n"
        "    image: alpine:3.20\n"
        '    command: ["sleep", "120"]\n'
        f'    volumes: ["{data_dir}:/data"]\n',
        encoding="utf-8",
    )
    return compose


def _init_and_backup(repo: Path, password_file: Path, data_dir: Path) -> None:
    from tests.helpers.restic_lab import init_and_backup_with_password_file

    init_and_backup_with_password_file(
        repo, password_file, data_dir, password="restore-lab-pass\n"
    )


def test_selected_restore_verification_pass(tmp_path: Path) -> None:
    data = tmp_path / "data"
    compose = _write_stack(tmp_path, data)
    repo = tmp_path / "repo"
    passfile = tmp_path / "restic.pass"
    _init_and_backup(repo, passfile, data)

    # Start container so discovery sees the bind.
    import subprocess

    subprocess.run(
        ["docker", "compose", "-f", str(compose), "up", "-d"],
        check=True,
        capture_output=True,
    )
    try:
        cfg = tmp_path / "backuplint.yml"
        cfg.write_text(
            f"backup_paths: []\n"
            f"restic:\n"
            f"  repository: {repo}\n"
            f"  password_file: {passfile}\n"
            f"  restore_verification:\n"
            f"    mode: selected\n",
            encoding="utf-8",
        )
        result = runner.invoke(
            app,
            ["scan", str(compose), "--config", str(cfg)],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "selected-path restore verification passed" in result.output
        assert "Result: PASS" in result.output
    finally:
        subprocess.run(
            ["docker", "compose", "-f", str(compose), "down"],
            check=False,
            capture_output=True,
        )


def test_restore_verify_cli_override_and_bad_password(tmp_path: Path) -> None:
    data = tmp_path / "data"
    compose = _write_stack(tmp_path, data)
    repo = tmp_path / "repo"
    passfile = tmp_path / "restic.pass"
    bad = tmp_path / "bad.pass"
    _init_and_backup(repo, passfile, data)
    bad.write_text("wrong\n", encoding="utf-8")
    bad.chmod(0o600)

    import subprocess

    subprocess.run(
        ["docker", "compose", "-f", str(compose), "up", "-d"],
        check=True,
        capture_output=True,
    )
    try:
        cfg = tmp_path / "backuplint.yml"
        cfg.write_text(
            f"backup_paths: []\n"
            f"restic:\n"
            f"  repository: {repo}\n"
            f"  password_file: {bad}\n"
            f"  restore_verification:\n"
            f"    mode: off\n",
            encoding="utf-8",
        )
        # Wrong password fails during snapshot listing before restore.
        result = runner.invoke(
            app,
            [
                "scan",
                str(compose),
                "--config",
                str(cfg),
                "--restore-verify",
                "selected",
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 2
        assert "authentication failed" in result.output.lower()
    finally:
        subprocess.run(
            ["docker", "compose", "-f", str(compose), "down"],
            check=False,
            capture_output=True,
        )
