"""Compose and CLI failure-mode tests for production readiness."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from backuplint.cli import app
from backuplint.compose import ComposeError, load_compose_config
from backuplint.database import is_database_image
from backuplint.process import TimeoutExpired

runner = CliRunner()


def test_compose_timeout(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}\n")
    with patch("backuplint.compose.shutil.which", return_value="/usr/bin/docker"):
        with patch(
            "backuplint.compose.run_argv",
            side_effect=TimeoutExpired(cmd=["docker"], timeout=60),
        ):
            with pytest.raises(ComposeError, match="timed out"):
                load_compose_config(compose)


def test_unexpected_compose_json_shape(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}\n")
    with patch("backuplint.compose.shutil.which", return_value="/usr/bin/docker"):
        with patch("backuplint.compose.run_argv") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "[]"
            run.return_value.stderr = ""
            with pytest.raises(ComposeError, match="unexpected"):
                load_compose_config(compose)


def test_database_exporters_not_flagged() -> None:
    assert is_database_image("prom/postgres-exporter") is False
    assert is_database_image("prom/mysqld-exporter") is False
    assert is_database_image("bitnami/postgres-exporter") is False
    assert is_database_image("library/postgres:16") is True
    assert is_database_image("mongo:7") is True


def test_json_output_is_pure_json(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n  app:\n    image: alpine:3.20\n"
        "    command: [\"true\"]\n    volumes: [\"./data:/data\"]\n"
    )
    config = tmp_path / "backuplint.yml"
    config.write_text(f"backup_paths:\n  - {data}\n")
    result = runner.invoke(app, ["scan", str(compose), "--config", str(config), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["result"] == "PASS"
    assert "BackupLint Audit" not in result.stdout


def test_metacharacter_bind_path_not_shell(tmp_path: Path) -> None:
    weird = tmp_path / "data; rm -rf slash"
    weird.mkdir()
    compose = tmp_path / "compose.yml"
    # Long syntax avoids Compose short-form colon splitting on unusual names.
    compose.write_text(
        "services:\n"
        "  app:\n"
        "    image: alpine:3.20\n"
        "    command: [\"true\"]\n"
        "    volumes:\n"
        "      - type: bind\n"
        f"        source: ./{weird.name}\n"
        "        target: /data\n"
    )
    config = tmp_path / "backuplint.yml"
    config.write_text(f"backup_paths:\n  - {weird}\n")
    result = runner.invoke(app, ["scan", str(compose), "--config", str(config)])
    assert result.exit_code == 0
    assert "PASS" in result.stdout
    assert weird.exists()
