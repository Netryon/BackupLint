"""Unit tests for Restic integrity check wrapper and reporting composition."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from backuplint.classify import StorageClass
from backuplint.config import IntegrityMode
from backuplint.coverage import CoverageFinding, CoverageStatus, evaluate_mount
from backuplint.models import Mount, MountType
from backuplint.process import TimeoutExpired
from backuplint.reporting import (
    AuditResult,
    format_audit_json,
    format_audit_report,
    summarize_findings,
)
from backuplint.restic import (
    IntegrityCheckResult,
    IntegrityStatus,
    ResticError,
    check_repository_integrity,
)


def _bind(service: str, source: str) -> Mount:
    return Mount(service=service, type=MountType.BIND, source=source, target="/data")


def test_check_off_is_not_requested() -> None:
    result = check_repository_integrity(mode=IntegrityMode.OFF, repository="/repo")
    assert result.status is IntegrityStatus.NOT_REQUESTED
    assert result.mode is IntegrityMode.OFF


def test_check_healthy_standard(tmp_path: Path) -> None:
    password_file = tmp_path / "pass"
    password_file.write_text("secret-value\n", encoding="utf-8")
    password_file.chmod(0o600)
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
    assert result.status is IntegrityStatus.PASSED
    assert result.mode is IntegrityMode.STANDARD
    check_calls = [
        c
        for c in run.call_args_list
        if c.args and c.args[0][:2] == ["/usr/bin/restic", "check"]
    ]
    assert len(check_calls) == 1
    env = check_calls[0].kwargs["env"]
    assert env["RESTIC_PASSWORD"] == "secret-value"
    assert "RESTIC_PASSWORD_FILE" not in env


def test_check_deep_uses_read_data(tmp_path: Path) -> None:
    password_file = tmp_path / "pass"
    password_file.write_text("x", encoding="utf-8")
    password_file.chmod(0o600)
    with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
        with patch("backuplint.restic.run_argv") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            run.return_value.stderr = ""
            check_repository_integrity(
                mode=IntegrityMode.DEEP,
                repository=str(tmp_path / "repo"),
                password_file=password_file,
            )
    check_calls = [c for c in run.call_args_list if c.args and "check" in c.args[0]]
    assert check_calls[0].args[0] == ["/usr/bin/restic", "check", "--read-data"]


def test_check_corruption_is_failed(tmp_path: Path) -> None:
    password_file = tmp_path / "pass"
    password_file.write_text("secret", encoding="utf-8")
    password_file.chmod(0o600)
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
    assert result.status is IntegrityStatus.FAILED
    assert "inconsistency" in result.message


def test_check_bad_password_is_error_and_scrubs_secret(tmp_path: Path) -> None:
    password_file = tmp_path / "pass"
    password_file.write_text("super-secret-password", encoding="utf-8")
    password_file.chmod(0o600)
    with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
        with patch("backuplint.restic.run_argv") as run:
            run.return_value.returncode = 1
            run.return_value.stdout = ""
            run.return_value.stderr = (
                "Fatal: wrong password or no key found for super-secret-password"
            )
            result = check_repository_integrity(
                mode=IntegrityMode.STANDARD,
                repository=str(tmp_path / "repo"),
                password_file=password_file,
            )
    assert result.status is IntegrityStatus.ERROR
    assert "authentication failed" in result.message
    assert "super-secret-password" not in result.message


def test_check_missing_repo_is_error(tmp_path: Path) -> None:
    password_file = tmp_path / "pass"
    password_file.write_text("x", encoding="utf-8")
    password_file.chmod(0o600)
    with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
        with patch("backuplint.restic.run_argv") as run:
            run.return_value.returncode = 10
            run.return_value.stdout = ""
            run.return_value.stderr = "Fatal: repository does not exist"
            result = check_repository_integrity(
                mode=IntegrityMode.STANDARD,
                repository=str(tmp_path / "missing"),
                password_file=password_file,
            )
    assert result.status is IntegrityStatus.ERROR
    assert "inaccessible or missing" in result.message


def test_check_locked_sanitizes_host_identity(tmp_path: Path) -> None:
    password_file = tmp_path / "pass"
    password_file.write_text("x", encoding="utf-8")
    password_file.chmod(0o600)
    with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
        with patch("backuplint.restic.run_argv") as run:
            run.return_value.returncode = 11
            run.return_value.stdout = ""
            run.return_value.stderr = (
                "unable to create lock in backend: repository is already locked "
                "exclusively by PID 1 on example-host by operator (UID 1000, GID 1000)"
            )
            result = check_repository_integrity(
                mode=IntegrityMode.STANDARD,
                repository=str(tmp_path / "repo"),
                password_file=password_file,
            )
    assert result.status is IntegrityStatus.ERROR
    assert "locked" in result.message
    assert "example-host" not in result.message
    assert "sysadmin" not in result.message


def test_check_empty_output_is_error_not_failed(tmp_path: Path) -> None:
    password_file = tmp_path / "pass"
    password_file.write_text("x", encoding="utf-8")
    password_file.chmod(0o600)
    with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
        with patch("backuplint.restic.run_argv") as run:
            run.return_value.returncode = 1
            run.return_value.stdout = ""
            run.return_value.stderr = ""
            result = check_repository_integrity(
                mode=IntegrityMode.DEEP,
                repository=str(tmp_path / "repo"),
                password_file=password_file,
            )
    assert result.status is IntegrityStatus.ERROR
    assert result.status is not IntegrityStatus.PASSED
    assert "locked" in result.message


def test_check_timeout_raises(tmp_path: Path) -> None:
    password_file = tmp_path / "pass"
    password_file.write_text("x", encoding="utf-8")
    password_file.chmod(0o600)
    with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
        with patch(
            "backuplint.restic.run_argv",
            side_effect=TimeoutExpired(cmd=["restic"], timeout=1),
        ):
            with pytest.raises(ResticError, match="timed out"):
                check_repository_integrity(
                    mode=IntegrityMode.STANDARD,
                    repository=str(tmp_path / "repo"),
                    password_file=password_file,
                    timeout=1,
                )


def test_check_missing_binary(tmp_path: Path) -> None:
    with patch("backuplint.restic.shutil.which", return_value=None):
        with pytest.raises(ResticError, match="not installed"):
            check_repository_integrity(
                mode=IntegrityMode.STANDARD,
                repository=str(tmp_path / "repo"),
                password="x",
            )


def test_check_oserror(tmp_path: Path) -> None:
    password_file = tmp_path / "pass"
    password_file.write_text("x", encoding="utf-8")
    password_file.chmod(0o600)
    with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
        with patch("backuplint.restic.run_argv", side_effect=OSError("boom")):
            with pytest.raises(ResticError, match="Failed to run Restic integrity"):
                check_repository_integrity(
                    mode=IntegrityMode.STANDARD,
                    repository=str(tmp_path / "repo"),
                    password_file=password_file,
                )


def test_check_unknown_nonzero_is_failed(tmp_path: Path) -> None:
    """Non-operational check failures are integrity FAIL across Restic versions."""
    password_file = tmp_path / "pass"
    password_file.write_text("x", encoding="utf-8")
    password_file.chmod(0o600)
    with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
        with patch("backuplint.restic.run_argv") as run:
            run.return_value.returncode = 1
            run.return_value.stdout = ""
            run.return_value.stderr = "error loading index: invalid data returned"
            result = check_repository_integrity(
                mode=IntegrityMode.STANDARD,
                repository=str(tmp_path / "repo"),
                password_file=password_file,
            )
    assert result.status is IntegrityStatus.FAILED
    assert "inconsistency" in result.message

    password_file = tmp_path / "pass"
    password_file.write_text("x", encoding="utf-8")
    password_file.chmod(0o600)
    with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
        with patch("backuplint.restic.run_argv") as run:
            run.return_value.returncode = 1
            run.return_value.stdout = "Fatal: repository contains errors"
            run.return_value.stderr = ""
            result = check_repository_integrity(
                mode=IntegrityMode.STANDARD,
                repository=str(tmp_path / "repo"),
                password_file=password_file,
            )
    assert result.status is IntegrityStatus.FAILED


def test_password_file_overrides_ambient_for_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    password_file = tmp_path / "config.pass"
    password_file.write_text("from-config\n", encoding="utf-8")
    password_file.chmod(0o600)
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", str(tmp_path / "ambient.pass"))
    monkeypatch.setenv("RESTIC_PASSWORD", "ambient-password")
    with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
        with patch("backuplint.restic.run_argv") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            run.return_value.stderr = ""
            check_repository_integrity(
                mode=IntegrityMode.STANDARD,
                repository=str(tmp_path / "repo"),
                password_file=password_file,
            )
    check_calls = [
        c
        for c in run.call_args_list
        if c.args and c.args[0][:2] == ["/usr/bin/restic", "check"]
    ]
    env = check_calls[0].kwargs["env"]
    assert env["RESTIC_PASSWORD"] == "from-config"
    assert "RESTIC_PASSWORD_FILE" not in env


def test_integrity_failure_forces_fail_result() -> None:
    findings = [evaluate_mount(_bind("web", "/srv/data"), ("/srv/data",))]
    integrity = IntegrityCheckResult(
        mode=IntegrityMode.STANDARD,
        status=IntegrityStatus.FAILED,
        duration_seconds=1.2,
        message="Restic repository integrity check failed: repository inconsistency detected.",
    )
    summary = summarize_findings(findings, integrity=integrity)
    assert summary.result is AuditResult.FAIL
    assert summary.exit_code == 1
    report = format_audit_report(findings, integrity=integrity)
    assert "Restic integrity" in report
    assert "standard integrity check failed" in report
    assert "Result: FAIL" in report
    payload = json.loads(format_audit_json(findings, integrity=integrity))
    assert payload["result"] == "FAIL"
    assert payload["integrity"]["status"] == "failed"
    assert payload["integrity"]["mode"] == "standard"


def test_integrity_pass_preserves_warn() -> None:
    finding = CoverageFinding(
        service="db",
        mount=None,
        status=CoverageStatus.UNSUPPORTED,
            storage_class=StorageClass.PERSISTENT,
        detail="Database workload detected (postgres).",
        host_path=None,
        covered_by=None,
    )
    integrity = IntegrityCheckResult(
        mode=IntegrityMode.STANDARD,
        status=IntegrityStatus.PASSED,
        duration_seconds=0.5,
        message="standard integrity check passed",
    )
    summary = summarize_findings([finding], integrity=integrity)
    assert summary.result is AuditResult.WARN
    assert summary.exit_code == 0


def test_coverage_fail_with_integrity_pass_still_fail() -> None:
    findings = [evaluate_mount(_bind("web", "/srv/miss"), ("/srv/other",))]
    integrity = IntegrityCheckResult(
        mode=IntegrityMode.STANDARD,
        status=IntegrityStatus.PASSED,
        duration_seconds=0.4,
        message="standard integrity check passed",
    )
    summary = summarize_findings(findings, integrity=integrity)
    assert summary.result is AuditResult.FAIL
    assert summary.exit_code == 1


def test_json_omits_integrity_when_not_requested() -> None:
    findings = [evaluate_mount(_bind("web", "/srv/data"), ("/srv/data",))]
    integrity = IntegrityCheckResult(
        mode=IntegrityMode.OFF,
        status=IntegrityStatus.NOT_REQUESTED,
        duration_seconds=0.0,
        message="Integrity verification was not requested.",
    )
    payload = json.loads(format_audit_json(findings, integrity=integrity))
    assert "integrity" not in payload
    assert payload["result"] == "PASS"
