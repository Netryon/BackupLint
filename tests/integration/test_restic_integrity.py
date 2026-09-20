"""Integration tests for Restic integrity verification and corruption labs."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backuplint.cli import app
from backuplint.config import IntegrityMode
from backuplint.restic import IntegrityStatus, check_repository_integrity
from tests.helpers.restic_lab import backup_paths, init_repo, restic_env_with_password

pytestmark = pytest.mark.skipif(
    shutil.which("restic") is None or shutil.which("docker") is None,
    reason="restic and docker are required",
)

runner = CliRunner()


def _init_repo(repo: Path, password: str) -> None:
    init_repo(repo, password)


def _backup(repo: Path, password: str, *paths: Path) -> None:
    backup_paths(repo, password, *paths)


def _restic_check(repo: Path, password: str) -> subprocess.CompletedProcess[str]:
    env = restic_env_with_password(password)
    return subprocess.run(
        ["restic", "-r", str(repo), "check"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _write_stack(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "data"
    data.mkdir()
    (data / "file.txt").write_text("integrity-lab\n", encoding="utf-8")
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n"
        "  web:\n"
        "    image: alpine:3.20\n"
        "    command: ['sleep', '30']\n"
        "    volumes:\n"
        "      - ./data:/data\n",
        encoding="utf-8",
    )
    return compose, data


def _write_config(tmp_path: Path, repo: Path, *, mode: str) -> Path:
    password_file = tmp_path / "restic.pass"
    if not password_file.exists():
        password_file.write_text("backuplint-integrity-pass", encoding="utf-8")
        password_file.chmod(0o600)
    config = tmp_path / "backuplint.yml"
    config.write_text(
        "backup_paths: []\n"
        "restic:\n"
        f"  repository: {repo}\n"
        "  password_file: restic.pass\n"
        "  integrity:\n"
        f"    mode: {mode}\n",
        encoding="utf-8",
    )
    return config


def _damage_index(repo: Path) -> None:
    """Damage repository metadata in a way standard `restic check` detects."""
    index_files = sorted(path for path in (repo / "index").rglob("*") if path.is_file())
    assert index_files, "expected index objects in disposable repository"
    target = index_files[0]
    target.chmod(0o644)
    # Corrupt index bytes. Deleting can also work; byte damage keeps object present.
    target.write_bytes(b"\x00\x01\x02corrupted-index")


def test_standard_integrity_pass_on_healthy_repo(tmp_path: Path) -> None:
    compose, data = _write_stack(tmp_path)
    repo = tmp_path / "repo"
    password = "backuplint-integrity-pass"
    _init_repo(repo, password)
    _backup(repo, password, data)
    config = _write_config(tmp_path, repo, mode="standard")

    result = runner.invoke(app, ["scan", str(compose), "--config", str(config)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Result: PASS" in result.stdout
    assert "Restic integrity" in result.stdout
    assert "standard integrity check passed" in result.stdout
    assert "standard" in result.stdout


def test_cli_integrity_override_standard(tmp_path: Path) -> None:
    compose, data = _write_stack(tmp_path)
    repo = tmp_path / "repo"
    password = "backuplint-integrity-pass"
    _init_repo(repo, password)
    _backup(repo, password, data)
    # Config leaves integrity off; CLI requests standard.
    config = _write_config(tmp_path, repo, mode="off")

    result = runner.invoke(
        app,
        ["scan", str(compose), "--config", str(config), "--integrity", "standard"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Restic integrity" in result.stdout


def test_integrity_json_includes_object(tmp_path: Path) -> None:
    compose, data = _write_stack(tmp_path)
    repo = tmp_path / "repo"
    password = "backuplint-integrity-pass"
    _init_repo(repo, password)
    _backup(repo, password, data)
    config = _write_config(tmp_path, repo, mode="standard")

    result = runner.invoke(
        app, ["scan", str(compose), "--config", str(config), "--json"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["result"] == "PASS"
    assert payload["integrity"]["mode"] == "standard"
    assert payload["integrity"]["status"] == "passed"


def test_corrupted_index_lab_standard_mode_fails(tmp_path: Path) -> None:
    """Disposable corruption lab: damaged index must not integrity-PASS."""
    compose, data = _write_stack(tmp_path)
    healthy = tmp_path / "healthy-repo"
    password = "backuplint-integrity-pass"
    _init_repo(healthy, password)
    _backup(healthy, password, data)

    # Confirm healthy path first on a copy used only for the positive check.
    healthy_config = _write_config(tmp_path, healthy, mode="standard")
    healthy_scan = runner.invoke(
        app, ["scan", str(compose), "--config", str(healthy_config)]
    )
    assert healthy_scan.exit_code == 0, healthy_scan.stdout + healthy_scan.stderr
    assert "Result: PASS" in healthy_scan.stdout

    damaged = tmp_path / "damaged-repo"
    shutil.copytree(healthy, damaged)
    # Ensure pack/index files are writable after copy.
    for path in damaged.rglob("*"):
        if path.is_file():
            path.chmod(0o644)
    _damage_index(damaged)

    direct = _restic_check(damaged, password)
    assert direct.returncode != 0, direct.stdout + direct.stderr

    # Point config at the disposable damaged copy only.
    damaged_config = _write_config(tmp_path, damaged, mode="standard")
    damaged_scan = runner.invoke(
        app, ["scan", str(compose), "--config", str(damaged_config)]
    )
    assert damaged_scan.exit_code == 1, damaged_scan.stdout + damaged_scan.stderr
    assert "Result: FAIL" in damaged_scan.stdout
    assert "standard integrity check failed" in damaged_scan.stdout
    assert "Result: PASS" not in damaged_scan.stdout

    wrapper = check_repository_integrity(
        mode=IntegrityMode.STANDARD,
        repository=str(damaged),
        password_file=tmp_path / "restic.pass",
    )
    assert wrapper.status is IntegrityStatus.FAILED


def test_missing_pack_lab_standard_mode_fails(tmp_path: Path) -> None:
    compose, data = _write_stack(tmp_path)
    healthy = tmp_path / "healthy-repo"
    password = "backuplint-integrity-pass"
    _init_repo(healthy, password)
    _backup(healthy, password, data)

    damaged = tmp_path / "damaged-repo"
    shutil.copytree(healthy, damaged)
    for path in damaged.rglob("*"):
        if path.is_file():
            path.chmod(0o644)
    packs = [path for path in (damaged / "data").rglob("*") if path.is_file()]
    assert packs, "expected data packs in disposable repository"
    packs[0].unlink()

    direct = _restic_check(damaged, password)
    assert direct.returncode != 0, direct.stdout + direct.stderr

    config = _write_config(tmp_path, damaged, mode="standard")
    result = runner.invoke(app, ["scan", str(compose), "--config", str(config)])
    assert result.exit_code == 1, result.stdout + result.stderr
    assert "Result: FAIL" in result.stdout
    assert "Result: PASS" not in result.stdout


def test_integrity_bad_password_exits_2(tmp_path: Path) -> None:
    compose, data = _write_stack(tmp_path)
    repo = tmp_path / "repo"
    password = "backuplint-integrity-pass"
    _init_repo(repo, password)
    _backup(repo, password, data)
    wrong = tmp_path / "restic.pass"
    wrong.write_text("wrong-password", encoding="utf-8")
    wrong.chmod(0o600)
    config = tmp_path / "backuplint.yml"
    config.write_text(
        "backup_paths: []\n"
        "restic:\n"
        f"  repository: {repo}\n"
        "  password_file: restic.pass\n"
        "  integrity:\n"
        "    mode: standard\n",
        encoding="utf-8",
    )
    # Snapshot listing fails first with bad password — still exit 2.
    result = runner.invoke(app, ["scan", str(compose), "--config", str(config)])
    assert result.exit_code == 2
    assert "authentication failed" in result.stderr


def _corrupt_pack_bytes(repo: Path) -> None:
    packs = [path for path in (repo / "data").rglob("*") if path.is_file()]
    assert packs, "expected data packs"
    target = packs[0]
    target.chmod(0o644)
    data = bytearray(target.read_bytes())
    mid = max(len(data) // 2, 1)
    for i in range(min(16, len(data) - mid)):
        data[mid + i] ^= 0xFF
    target.write_bytes(data)


def test_deep_integrity_pass_on_healthy_repo(tmp_path: Path) -> None:
    compose, data = _write_stack(tmp_path)
    repo = tmp_path / "repo"
    password = "backuplint-integrity-pass"
    _init_repo(repo, password)
    _backup(repo, password, data)
    config = _write_config(tmp_path, repo, mode="deep")
    result = runner.invoke(app, ["scan", str(compose), "--config", str(config)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "deep data integrity check passed" in result.stdout
    assert "deep" in result.stdout


def test_deep_detects_pack_corruption(tmp_path: Path) -> None:
    """Deep mode must use --read-data and fail on disposable pack damage."""
    compose, data = _write_stack(tmp_path)
    healthy = tmp_path / "healthy"
    password = "backuplint-integrity-pass"
    _init_repo(healthy, password)
    _backup(healthy, password, data)
    damaged = tmp_path / "damaged"
    shutil.copytree(healthy, damaged)
    for path in damaged.rglob("*"):
        if path.is_file():
            path.chmod(0o644)
    _corrupt_pack_bytes(damaged)

    # Direct deep check fails.
    env = restic_env_with_password(password)
    direct = subprocess.run(
        ["restic", "-r", str(damaged), "check", "--read-data"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert direct.returncode != 0, direct.stdout + direct.stderr

    config = _write_config(tmp_path, damaged, mode="deep")
    result = runner.invoke(app, ["scan", str(compose), "--config", str(config)])
    assert result.exit_code == 1, result.stdout + result.stderr
    assert "Result: FAIL" in result.stdout
    assert "deep data integrity check failed" in result.stdout

    wrapper = check_repository_integrity(
        mode=IntegrityMode.DEEP,
        repository=str(damaged),
        password_file=tmp_path / "restic.pass",
    )
    assert wrapper.status is IntegrityStatus.FAILED
    assert wrapper.mode is IntegrityMode.DEEP
