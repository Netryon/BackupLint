"""Unit tests for Restic snapshot parsing and sanitization."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from backuplint.restic import (
    ResticError,
    latest_relevant_snapshot,
    latest_snapshot,
    list_snapshots,
    parse_snapshots_json,
)

SAMPLE = [
    {
        "time": "2026-01-01T10:00:00Z",
        "paths": ["/srv/a"],
        "id": "aaaa1111bbbb2222",
        "short_id": "aaaa1111",
    },
    {
        "time": "2026-01-02T10:00:00Z",
        "paths": ["/srv/a", "/srv/b"],
        "id": "cccc3333dddd4444",
        "short_id": "cccc3333",
    },
]


def test_parse_snapshots_json() -> None:
    snapshots = parse_snapshots_json(json.dumps(SAMPLE))
    assert len(snapshots) == 2
    assert snapshots[1].paths == ("/srv/a", "/srv/b")
    assert snapshots[1].time == datetime(2026, 1, 2, 10, 0, tzinfo=UTC)


def test_parse_empty_list() -> None:
    assert parse_snapshots_json("[]") == []


def test_parse_empty_payload_is_error_not_pass() -> None:
    with pytest.raises(ResticError, match="empty snapshot JSON"):
        parse_snapshots_json("")
    with pytest.raises(ResticError, match="empty snapshot JSON"):
        parse_snapshots_json("   \n")


def test_parse_invalid_shape() -> None:
    with pytest.raises(ResticError, match="must be a list"):
        parse_snapshots_json('{"paths": []}')


def test_latest_snapshot_by_time() -> None:
    snapshots = parse_snapshots_json(json.dumps(SAMPLE))
    latest = latest_snapshot(snapshots)
    assert latest is not None
    assert latest.short_id == "cccc3333"


def test_latest_relevant_ignores_unrelated_newer_snapshot() -> None:
    payload = [
        {
            "time": "2026-01-01T10:00:00Z",
            "paths": ["/srv/docker"],
            "id": "dock",
            "short_id": "dock",
        },
        {
            "time": "2026-01-01T11:00:00Z",
            "paths": ["/etc"],
            "id": "etc1",
            "short_id": "etc1",
        },
    ]
    snapshots = parse_snapshots_json(json.dumps(payload))
    assert latest_snapshot(snapshots).short_id == "etc1"
    relevant = latest_relevant_snapshot(snapshots, "/srv/docker/sonarr")
    assert relevant is not None
    assert relevant.short_id == "dock"


def test_malformed_timestamp_becomes_none() -> None:
    payload = [
        {
            "time": "not-a-timestamp",
            "paths": ["/srv/docker"],
            "id": "badtime",
            "short_id": "badtime",
        }
    ]
    snapshots = parse_snapshots_json(json.dumps(payload))
    assert snapshots[0].time is None


def test_list_snapshots_success(tmp_path: Path) -> None:
    password_file = tmp_path / "pass"
    password_file.write_text("secret-value\n", encoding="utf-8")
    password_file.chmod(0o600)
    with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
        with patch("backuplint.restic.run_argv") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = json.dumps(SAMPLE)
            run.return_value.stderr = ""
            snapshots = list_snapshots(
                repository=str(tmp_path / "repo"),
                password_file=password_file,
            )
    assert len(snapshots) == 2
    env = run.call_args.kwargs["env"]
    assert env["RESTIC_PASSWORD"] == "secret-value"
    assert env["RESTIC_REPOSITORY"] == str(tmp_path / "repo")


def test_list_snapshots_auth_failure_hides_secret(tmp_path: Path) -> None:
    password_file = tmp_path / "pass"
    password_file.write_text("super-secret-password", encoding="utf-8")
    password_file.chmod(0o600)
    with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
        with patch("backuplint.restic.run_argv") as run:
            run.return_value.returncode = 1
            run.return_value.stdout = ""
            run.return_value.stderr = "wrong password or no key found for super-secret-password"
            with pytest.raises(ResticError, match="authentication failed") as exc:
                list_snapshots(
                    repository=str(tmp_path / "repo"),
                    password_file=password_file,
                )
    assert "super-secret-password" not in exc.value.message


def test_list_snapshots_config_password_overrides_ambient_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured password_file must win over ambient RESTIC_PASSWORD_FILE.

    Otherwise a wrong password_file in backuplint.yml could still open the
    repository via a leftover environment password file (false PASS risk).
    """
    password_file = tmp_path / "config.pass"
    password_file.write_text("from-config\n", encoding="utf-8")
    password_file.chmod(0o600)
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", str(tmp_path / "ambient.pass"))
    monkeypatch.setenv("RESTIC_PASSWORD", "ambient-password")
    with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
        with patch("backuplint.restic.run_argv") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "[]"
            run.return_value.stderr = ""
            list_snapshots(repository=str(tmp_path / "repo"), password_file=password_file)
    env = run.call_args.kwargs["env"]
    assert env["RESTIC_PASSWORD"] == "from-config"
    assert "RESTIC_PASSWORD_FILE" not in env


def test_list_snapshots_wrong_config_password_not_rescued_by_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    password_file = tmp_path / "wrong.pass"
    password_file.write_text("wrong-password-value\n", encoding="utf-8")
    password_file.chmod(0o600)
    ambient = tmp_path / "ambient.pass"
    ambient.write_text("correct-secret\n", encoding="utf-8")
    ambient.chmod(0o600)
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", str(ambient))
    monkeypatch.delenv("RESTIC_PASSWORD", raising=False)
    with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
        with patch("backuplint.restic.run_argv") as run:
            run.return_value.returncode = 1
            run.return_value.stdout = ""
            run.return_value.stderr = "wrong password or no key found"
            with pytest.raises(ResticError, match="authentication failed") as exc:
                list_snapshots(
                    repository=str(tmp_path / "repo"),
                    password_file=password_file,
                )
    env = run.call_args.kwargs["env"]
    assert env["RESTIC_PASSWORD"] == "wrong-password-value"
    assert "RESTIC_PASSWORD_FILE" not in env
    assert "wrong-password-value" not in exc.value.message
    monkeypatch.setenv("RESTIC_PASSWORD", "env-only-secret")
    monkeypatch.delenv("RESTIC_PASSWORD_FILE", raising=False)
    with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
        with patch("backuplint.restic.run_argv") as run:
            run.return_value.returncode = 1
            run.return_value.stdout = ""
            run.return_value.stderr = "fatal: wrong password env-only-secret"
            with pytest.raises(ResticError) as exc:
                list_snapshots(repository=str(tmp_path / "repo"))
    assert "env-only-secret" not in exc.value.message


def test_list_snapshots_timeout(tmp_path: Path) -> None:
    from backuplint.process import TimeoutExpired

    password_file = tmp_path / "pass"
    password_file.write_text("x", encoding="utf-8")
    password_file.chmod(0o600)
    with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
        with patch(
            "backuplint.restic.run_argv",
            side_effect=TimeoutExpired(cmd=["restic"], timeout=120),
        ):
            with pytest.raises(ResticError, match="timed out"):
                list_snapshots(
                    repository=str(tmp_path / "repo"),
                    password_file=password_file,
                )


def test_list_snapshots_broken_password_symlink(tmp_path: Path) -> None:
    link = tmp_path / "missing-pass"
    link.symlink_to(tmp_path / "does-not-exist")
    with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
        with pytest.raises(ResticError, match="broken symlink|not found|secret file"):
            list_snapshots(repository=str(tmp_path / "repo"), password_file=link)


def test_list_snapshots_missing_password(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RESTIC_PASSWORD", raising=False)
    monkeypatch.delenv("RESTIC_PASSWORD_FILE", raising=False)
    with patch("backuplint.restic.shutil.which", return_value="/usr/bin/restic"):
        with pytest.raises(ResticError, match="password not provided"):
            list_snapshots(repository=str(tmp_path / "repo"))


def test_list_snapshots_restic_missing(tmp_path: Path) -> None:
    with patch("backuplint.restic.shutil.which", return_value=None):
        with pytest.raises(ResticError, match="not installed"):
            list_snapshots(repository=str(tmp_path / "repo"), password="x")
