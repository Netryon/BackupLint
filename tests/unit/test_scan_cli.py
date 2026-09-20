"""CLI tests for scan coverage behavior."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backuplint.cli import app

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "compose"
runner = CliRunner()

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="Docker is required for scan CLI tests",
)


def _write_stack(tmp_path: Path) -> tuple[Path, Path]:
    compose = tmp_path / "compose.yml"
    compose.write_text((FIXTURES / "one-service-bind.yml").read_text())
    (tmp_path / "sonarr").mkdir()
    config = tmp_path / "backuplint.yml"
    return compose, config


def test_scan_protected_exits_zero(tmp_path: Path) -> None:
    compose, config = _write_stack(tmp_path)
    config.write_text(f"backup_paths:\n  - {tmp_path}\n")
    result = runner.invoke(app, ["scan", str(compose), "--config", str(config)])
    assert result.exit_code == 0
    assert "BackupLint Audit" in result.stdout
    assert "Result: PASS" in result.stdout
    assert "sonarr" in result.stdout


def test_scan_missing_coverage_exits_one(tmp_path: Path) -> None:
    compose, config = _write_stack(tmp_path)
    config.write_text("backup_paths:\n  - /srv/unrelated\n")
    result = runner.invoke(app, ["scan", str(compose), "--config", str(config)])
    assert result.exit_code == 1
    assert "Result: FAIL" in result.stdout
    assert "critical" in result.stdout


def test_scan_json_output(tmp_path: Path) -> None:
    compose, config = _write_stack(tmp_path)
    config.write_text(f"backup_paths:\n  - {tmp_path}\n")
    result = runner.invoke(
        app,
        ["scan", str(compose), "--config", str(config), "--json"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["result"] == "PASS"
    assert payload["summary"]["protected"] >= 1


def test_scan_similar_path_not_protected(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n"
        "  app2:\n"
        "    image: alpine:3.20\n"
        "    volumes:\n"
        "      - ./app2:/config\n"
    )
    (tmp_path / "app2").mkdir()
    # Configure backup for ./app which must not cover ./app2
    config = tmp_path / "backuplint.yml"
    config.write_text(f"backup_paths:\n  - {tmp_path / 'app'}\n")
    result = runner.invoke(app, ["scan", str(compose), "--config", str(config)])
    assert result.exit_code == 1
    assert "not protected" in result.stdout


def test_scan_missing_config_exits_two(tmp_path: Path) -> None:
    compose, _config = _write_stack(tmp_path)
    result = runner.invoke(
        app,
        ["scan", str(compose), "--config", str(tmp_path / "missing.yml")],
    )
    assert result.exit_code == 2
    assert "not found" in (result.stdout + result.stderr).lower()


def test_scan_malformed_config_exits_two(tmp_path: Path) -> None:
    compose, config = _write_stack(tmp_path)
    config.write_text("backup_paths: [\n")
    result = runner.invoke(app, ["scan", str(compose), "--config", str(config)])
    assert result.exit_code == 2
    assert "malformed" in (result.stdout + result.stderr).lower()


def test_scan_missing_compose_still_fails_cleanly(tmp_path: Path) -> None:
    config = tmp_path / "backuplint.yml"
    config.write_text("backup_paths:\n  - /srv/docker\n")
    result = runner.invoke(
        app,
        ["scan", str(tmp_path / "nope.yml"), "--config", str(config)],
    )
    assert result.exit_code == 2
    assert "not found" in (result.stdout + result.stderr).lower()
    assert "Traceback" not in (result.stdout + result.stderr)
