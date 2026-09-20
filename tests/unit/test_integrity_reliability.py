"""Performance and reliability checks for integrity verification."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from backuplint.cli import app
from backuplint.config import IntegrityMode
from backuplint.models import Mount, MountType, ServiceMounts
from backuplint.restic import (
    IntegrityCheckResult,
    IntegrityStatus,
    ResticSnapshot,
    check_repository_integrity,
)

runner = CliRunner()


def test_integrity_runs_once_for_many_mounts(tmp_path: Path) -> None:
    services = [
        ServiceMounts(
            name=f"s{i}",
            mounts=(
                Mount(
                    service=f"s{i}",
                    type=MountType.BIND,
                    source=str(tmp_path / f"data-{i}"),
                    target="/data",
                ),
            ),
        )
        for i in range(50)
    ]
    for service in services:
        path = Path(service.mounts[0].source)
        path.mkdir(parents=True, exist_ok=True)

    password_file = tmp_path / "restic.pass"
    password_file.write_text("x", encoding="utf-8")
    password_file.chmod(0o600)
    config = tmp_path / "backuplint.yml"
    config.write_text(
        "backup_paths: []\n"
        "restic:\n"
        f"  repository: {tmp_path / 'repo'}\n"
        "  password_file: restic.pass\n"
        "  integrity:\n"
        "    mode: standard\n",
        encoding="utf-8",
    )
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")

    snapshots = [
        ResticSnapshot(
            snapshot_id="abc",
            short_id="abc",
            time=None,
            paths=tuple(str(tmp_path / f"data-{i}") for i in range(50)),
        )
    ]
    integrity = IntegrityCheckResult(
        mode=IntegrityMode.STANDARD,
        status=IntegrityStatus.PASSED,
        duration_seconds=0.2,
        message="standard integrity check passed",
        requested=True,
    )
    with patch("backuplint.audit.discover_mounts", return_value=services):
        with patch("backuplint.audit.list_snapshots", return_value=snapshots) as list_snaps:
            with patch(
                "backuplint.audit.check_repository_integrity",
                return_value=integrity,
            ) as check:
                result = runner.invoke(
                    app, ["scan", str(compose), "--config", str(config)]
                )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert list_snaps.call_count == 1
    assert check.call_count == 1
    assert "Restic integrity" in result.stdout


def test_repeated_pass_is_stable(tmp_path: Path) -> None:
    password_file = tmp_path / "pass"
    password_file.write_text("x", encoding="utf-8")
    password_file.chmod(0o600)
    outcomes = []
    for _ in range(5):
        with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
            with patch("backuplint.restic.run_argv") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = "no errors were found\n"
                run.return_value.stderr = ""
                result = check_repository_integrity(
                    mode=IntegrityMode.STANDARD,
                    repository=str(tmp_path / "repo"),
                    password_file=password_file,
                )
        outcomes.append((result.status, result.message))
    assert len(set(outcomes)) == 1
    assert outcomes[0][0] is IntegrityStatus.PASSED


def test_repeated_corruption_fail_is_stable(tmp_path: Path) -> None:
    password_file = tmp_path / "pass"
    password_file.write_text("x", encoding="utf-8")
    password_file.chmod(0o600)
    outcomes = []
    for _ in range(5):
        with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
            with patch("backuplint.restic.run_argv") as run:
                run.return_value.returncode = 1
                run.return_value.stdout = ""
                run.return_value.stderr = "Fatal: repository contains errors"
                result = check_repository_integrity(
                    mode=IntegrityMode.STANDARD,
                    repository=str(tmp_path / "repo"),
                    password_file=password_file,
                )
        outcomes.append((result.status, result.message))
    assert len(set(outcomes)) == 1
    assert outcomes[0][0] is IntegrityStatus.FAILED


def test_repeated_auth_error_is_stable(tmp_path: Path) -> None:
    password_file = tmp_path / "pass"
    password_file.write_text("secret-pass", encoding="utf-8")
    password_file.chmod(0o600)
    outcomes = []
    for _ in range(5):
        with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
            with patch("backuplint.restic.run_argv") as run:
                run.return_value.returncode = 12
                run.return_value.stdout = ""
                run.return_value.stderr = "wrong password secret-pass"
                result = check_repository_integrity(
                    mode=IntegrityMode.STANDARD,
                    repository=str(tmp_path / "repo"),
                    password_file=password_file,
                )
        outcomes.append((result.status, result.message))
        assert "secret-pass" not in result.message
    assert len(set(outcomes)) == 1
    assert outcomes[0][0] is IntegrityStatus.ERROR
