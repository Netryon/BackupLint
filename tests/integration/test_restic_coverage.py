"""Integration tests against disposable local Restic repositories."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backuplint.cli import app
from backuplint.restic import list_snapshots
from tests.helpers.restic_lab import backup_paths, init_repo

pytestmark = pytest.mark.skipif(
    shutil.which("restic") is None or shutil.which("docker") is None,
    reason="restic and docker are required",
)

runner = CliRunner()


def _init_repo(repo: Path, password: str) -> None:
    init_repo(repo, password)


def _backup(repo: Path, password: str, *paths: Path) -> None:
    backup_paths(repo, password, *paths)


def _write_stack(tmp_path: Path) -> tuple[Path, Path, Path]:
    data_a = tmp_path / "data-a"
    data_b = tmp_path / "data-b"
    data_a.mkdir()
    data_b.mkdir()
    (data_a / "file.txt").write_text("a\n")
    (data_b / "file.txt").write_text("b\n")
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n"
        "  a:\n"
        "    image: alpine:3.20\n"
        "    command: ['sleep', '30']\n"
        "    volumes:\n"
        "      - ./data-a:/data\n"
        "  b:\n"
        "    image: alpine:3.20\n"
        "    command: ['sleep', '30']\n"
        "    volumes:\n"
        "      - ./data-b:/data\n"
    )
    return compose, data_a, data_b


def test_complete_snapshot_covers_mounts(tmp_path: Path) -> None:
    compose, data_a, data_b = _write_stack(tmp_path)
    repo = tmp_path / "repo"
    password = "backuplint-test-pass"
    password_file = tmp_path / "restic.pass"
    password_file.write_text(password, encoding="utf-8")
    password_file.chmod(0o600)
    _init_repo(repo, password)
    _backup(repo, password, data_a, data_b)

    config = tmp_path / "backuplint.yml"
    config.write_text(
        "backup_paths: []\n"
        "restic:\n"
        f"  repository: {repo}\n"
        "  password_file: restic.pass\n"
    )

    result = runner.invoke(app, ["scan", str(compose), "--config", str(config)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Result: PASS" in result.stdout
    assert "snapshot" in result.stdout


def test_omitted_path_in_snapshot_fails(tmp_path: Path) -> None:
    compose, data_a, data_b = _write_stack(tmp_path)
    repo = tmp_path / "repo"
    password = "backuplint-test-pass"
    password_file = tmp_path / "restic.pass"
    password_file.write_text(password, encoding="utf-8")
    password_file.chmod(0o600)
    _init_repo(repo, password)
    _backup(repo, password, data_a)  # omit data_b

    config = tmp_path / "backuplint.yml"
    config.write_text(
        "backup_paths: []\n"
        "restic:\n"
        f"  repository: {repo}\n"
        "  password_file: restic.pass\n"
    )
    result = runner.invoke(app, ["scan", str(compose), "--config", str(config)])
    assert result.exit_code == 1
    assert "Result: FAIL" in result.stdout
    assert "data-b" in result.stdout


def test_no_snapshots_fails(tmp_path: Path) -> None:
    compose, _a, _b = _write_stack(tmp_path)
    repo = tmp_path / "repo"
    password = "backuplint-test-pass"
    password_file = tmp_path / "restic.pass"
    password_file.write_text(password, encoding="utf-8")
    password_file.chmod(0o600)
    _init_repo(repo, password)

    config = tmp_path / "backuplint.yml"
    config.write_text(
        "backup_paths: []\n"
        "restic:\n"
        f"  repository: {repo}\n"
        "  password_file: restic.pass\n"
    )
    result = runner.invoke(app, ["scan", str(compose), "--config", str(config)])
    assert result.exit_code == 1
    assert "no Restic snapshots found" in result.stdout


def test_wrong_password_exits_error(tmp_path: Path) -> None:
    compose, data_a, _b = _write_stack(tmp_path)
    repo = tmp_path / "repo"
    _init_repo(repo, "correct-password")
    _backup(repo, "correct-password", data_a)
    password_file = tmp_path / "restic.pass"
    password_file.write_text("wrong-password", encoding="utf-8")
    password_file.chmod(0o600)
    config = tmp_path / "backuplint.yml"
    config.write_text(
        "backup_paths: []\n"
        "restic:\n"
        f"  repository: {repo}\n"
        "  password_file: restic.pass\n"
    )
    result = runner.invoke(app, ["scan", str(compose), "--config", str(config)])
    combined = result.stdout + result.stderr
    assert result.exit_code == 2
    assert "authentication failed" in combined.lower()
    assert "wrong-password" not in combined
    assert "correct-password" not in combined


def test_inaccessible_repository_exits_error(tmp_path: Path) -> None:
    compose, _a, _b = _write_stack(tmp_path)
    password_file = tmp_path / "restic.pass"
    password_file.write_text("x", encoding="utf-8")
    password_file.chmod(0o600)
    config = tmp_path / "backuplint.yml"
    missing_repo = tmp_path / "missing-repo"
    config.write_text(
        "backup_paths: []\n"
        "restic:\n"
        f"  repository: {missing_repo}\n"
        "  password_file: restic.pass\n"
    )
    result = runner.invoke(app, ["scan", str(compose), "--config", str(config)])
    assert result.exit_code == 2
    assert "inaccessible" in (result.stdout + result.stderr).lower() or "unable to open" in (
        result.stdout + result.stderr
    ).lower()


def test_multiple_snapshots_uses_latest(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    password = "backuplint-test-pass"
    _init_repo(repo, password)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _backup(repo, password, first)
    _backup(repo, password, first, second)
    snapshots = list_snapshots(repository=str(repo), password=password)
    from backuplint.restic import latest_relevant_snapshot

    relevant = latest_relevant_snapshot(snapshots, str(second))
    assert relevant is not None
    assert any(Path(path) == second or str(second) == path for path in relevant.paths)


def test_unrelated_newer_snapshot_does_not_hide_path_coverage(tmp_path: Path) -> None:
    compose, data_a, data_b = _write_stack(tmp_path)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    repo = tmp_path / "repo"
    password = "backuplint-test-pass"
    password_file = tmp_path / "restic.pass"
    password_file.write_text(password, encoding="utf-8")
    password_file.chmod(0o600)
    _init_repo(repo, password)
    _backup(repo, password, data_a, data_b)
    _backup(repo, password, unrelated)

    config = tmp_path / "backuplint.yml"
    config.write_text(
        "backup_paths: []\n"
        "restic:\n"
        f"  repository: {repo}\n"
        "  password_file: restic.pass\n"
    )
    result = runner.invoke(app, ["scan", str(compose), "--config", str(config)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Result: PASS" in result.stdout


def test_stale_snapshot_warns_with_max_backup_age(tmp_path: Path) -> None:
    compose, data_a, data_b = _write_stack(tmp_path)
    repo = tmp_path / "repo"
    password = "backuplint-test-pass"
    password_file = tmp_path / "restic.pass"
    password_file.write_text(password, encoding="utf-8")
    password_file.chmod(0o600)
    _init_repo(repo, password)
    _backup(repo, password, data_a, data_b)

    config = tmp_path / "backuplint.yml"
    config.write_text(
        "backup_paths: []\n"
        "max_backup_age: 1s\n"
        "restic:\n"
        f"  repository: {repo}\n"
        "  password_file: restic.pass\n"
    )
    import time

    time.sleep(1.1)
    result = runner.invoke(app, ["scan", str(compose), "--config", str(config)])
    assert result.exit_code == 0
    assert "Result: WARN" in result.stdout
    assert "stale" in result.stdout
