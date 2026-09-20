"""Security-focused tests for integrity checking and state handling."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from backuplint.config import ConfigError, IntegrityMode, parse_config_data
from backuplint.integrity_state import (
    record_integrity_success,
    repository_state_key,
)
from backuplint.process import run_argv
from backuplint.reporting import format_audit_json
from backuplint.restic import IntegrityCheckResult, IntegrityStatus, check_repository_integrity


def test_no_shell_or_eval_in_application_code() -> None:
    for path in Path("src/backuplint").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "os.system(" not in text
        assert "eval(" not in text
        assert "exec(" not in text
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Allow documentation mentions inside comments/docstrings only.
            if 'shell=True' in line and not (
                stripped.startswith('"""')
                or stripped.startswith("'''")
                or "never" in line.lower()
                or "``shell=True``" in line
            ):
                raise AssertionError(f"{path}: unexpected shell=True usage: {line}")


def test_repository_metacharacters_stay_in_env_not_shell(tmp_path: Path) -> None:
    password_file = tmp_path / "pass"
    password_file.write_text("x", encoding="utf-8")
    password_file.chmod(0o600)
    repo = str(tmp_path / 'repo; rm -rf / -- "quoted"')
    with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
        with patch("backuplint.restic.run_argv") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            run.return_value.stderr = ""
            check_repository_integrity(
                mode=IntegrityMode.STANDARD,
                repository=repo,
                password_file=password_file,
            )
    check_calls = [
        c
        for c in run.call_args_list
        if c.args and c.args[0][:2] == ["/usr/bin/restic", "check"]
    ]
    assert check_calls[0].args[0] == ["/usr/bin/restic", "check"]
    assert check_calls[0].kwargs["env"]["RESTIC_REPOSITORY"] == repo


def test_state_file_path_with_spaces(tmp_path: Path) -> None:
    state = tmp_path / "my state dir" / "integrity state.json"
    record_integrity_success(
        state,
        repository="/repo",
        mode=IntegrityMode.STANDARD,
        checked_at=datetime.now(UTC),
    )
    assert state.is_file()
    assert "password" not in state.read_text(encoding="utf-8")


def test_state_and_json_omit_secrets_and_raw_repo_urls(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    record_integrity_success(
        state,
        repository="sftp://user:hunter2@backup.example/restic",
        mode=IntegrityMode.DEEP,
        checked_at=datetime(2026, 9, 8, tzinfo=UTC),
        restic_version="0.18.1",
    )
    body = state.read_text(encoding="utf-8")
    assert "hunter2" not in body
    assert "user:" not in body
    key = repository_state_key("sftp://user:hunter2@backup.example/restic")
    assert key in body

    integrity = IntegrityCheckResult(
        mode=IntegrityMode.STANDARD,
        status=IntegrityStatus.FAILED,
        duration_seconds=1.0,
        message=(
            "Restic repository integrity check failed: "
            "repository inconsistency detected."
        ),
        requested=True,
    )
    payload = json.loads(format_audit_json([], integrity=integrity))
    dumped = json.dumps(payload)
    assert "hunter2" not in dumped
    assert "RESTIC_PASSWORD" not in dumped


def test_malicious_restic_error_scrubs_secret(tmp_path: Path) -> None:
    password_file = tmp_path / "pass"
    password_file.write_text("top-secret-value", encoding="utf-8")
    password_file.chmod(0o600)
    with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
        with patch("backuplint.restic.run_argv") as run:
            run.return_value.returncode = 1
            run.return_value.stdout = ""
            run.return_value.stderr = (
                "Fatal: wrong password or no key found; leaked=top-secret-value"
            )
            result = check_repository_integrity(
                mode=IntegrityMode.STANDARD,
                repository=str(tmp_path / "repo"),
                password_file=password_file,
            )
    assert result.status is IntegrityStatus.ERROR
    assert "top-secret-value" not in result.message


def test_config_rejects_integrity_list_type() -> None:
    with pytest.raises(ConfigError):
        parse_config_data(
            {
                "backup_paths": [],
                "restic": {"repository": "/repo", "integrity": ["standard"]},
            }
        )


def test_run_argv_helper_is_list_only() -> None:
    completed = run_argv(["/bin/true"], timeout=5)
    assert completed.returncode == 0
