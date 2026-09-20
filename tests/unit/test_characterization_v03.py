"""Characterization tests locking v0.3 CLI/config/error classification seams."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backuplint import __version__
from backuplint.cli import app
from backuplint.config import ConfigError, load_config
from backuplint.restic_errors import (
    ResticOperationalKind,
    classify_integrity_operational,
    classify_list_snapshots_failure,
    classify_restore_operational,
    integrity_error_message,
    list_snapshots_error_message,
    restore_error_message,
)

runner = CliRunner()


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def test_scan_help_lists_integrity_and_restore_options() -> None:
    result = runner.invoke(app, ["scan", "--help"])
    assert result.exit_code == 0
    out = _plain(result.stdout)
    assert "--integrity" in out
    assert "--restore-verify" in out
    assert "--json" in out
    assert "--config" in out


def test_invalid_integrity_mode_exits_error(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n  a:\n    image: alpine:3.20\n    command: ['sleep', '1']\n"
    )
    config = tmp_path / "backuplint.yml"
    config.write_text("backup_paths: []\n")
    result = runner.invoke(
        app,
        ["scan", str(compose), "--config", str(config), "--integrity", "nope"],
    )
    assert result.exit_code == 2


def test_integrity_without_restic_section_exits_error(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n  a:\n    image: alpine:3.20\n    command: ['sleep', '1']\n"
    )
    config = tmp_path / "backuplint.yml"
    config.write_text("backup_paths: []\n")
    result = runner.invoke(
        app,
        ["scan", str(compose), "--config", str(config), "--integrity", "standard"],
    )
    assert result.exit_code == 2
    assert "restic" in ((result.stdout or "") + (result.stderr or "")).lower()


def test_unknown_config_key_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "backuplint.yml"
    cfg.write_text("backup_paths: []\nunexpected_top: true\n")
    with pytest.raises(ConfigError, match="Unknown configuration key"):
        load_config(cfg)


def test_json_contract_keys_when_protected(tmp_path: Path) -> None:
    """Semantic JSON keys remain stable for a simple protected scan."""
    import shutil

    if shutil.which("docker") is None:
        pytest.skip("docker required")
    data = tmp_path / "data"
    data.mkdir()
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n"
        "  app:\n"
        "    image: alpine:3.20\n"
        "    command: ['sleep', '30']\n"
        f"    volumes: ['{data}:/data']\n"
    )
    config = tmp_path / "backuplint.yml"
    config.write_text(f"backup_paths:\n  - {data}\n")
    result = runner.invoke(
        app, ["scan", str(compose), "--config", str(config), "--json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert set(payload.keys()) >= {"result", "summary", "findings"}
    assert payload["result"] == "PASS"
    assert "protected" in payload["summary"]
    assert "critical" in payload["summary"]


@pytest.mark.parametrize(
    ("output", "kind"),
    [
        ("Fatal: wrong password or no key found", ResticOperationalKind.AUTH),
        ("repository does not exist", ResticOperationalKind.NOT_FOUND),
        ("dial tcp 127.0.0.1:9: connection refused", ResticOperationalKind.NETWORK),
    ],
)
def test_list_snapshots_classification(
    output: str, kind: ResticOperationalKind
) -> None:
    assert classify_list_snapshots_failure(output) is kind
    assert list_snapshots_error_message(kind, output)


@pytest.mark.parametrize(
    ("returncode", "output", "kind"),
    [
        (12, "x", ResticOperationalKind.AUTH),
        (11, "unable to create lock", ResticOperationalKind.LOCKED),
        (10, "is not a repository", ResticOperationalKind.NOT_FOUND),
        (1, "connection reset by peer", ResticOperationalKind.NETWORK),
        (1, "pack id does not match", None),
    ],
)
def test_integrity_classification(
    returncode: int, output: str, kind: ResticOperationalKind | None
) -> None:
    assert classify_integrity_operational(returncode=returncode, output=output) is kind
    if kind is not None:
        msg = integrity_error_message(kind)
        assert "Unable to verify Restic repository integrity" in msg


@pytest.mark.parametrize(
    ("returncode", "output", "kind"),
    [
        (12, "wrong password", ResticOperationalKind.AUTH),
        (11, "already locked", ResticOperationalKind.LOCKED),
        (10, "unable to open config file", ResticOperationalKind.NOT_FOUND),
        (1, "permission denied", ResticOperationalKind.PERMISSION),
        (1, "tls handshake timeout", ResticOperationalKind.NETWORK),
        (1, "ciphertext verification failed", None),
    ],
)
def test_restore_classification(
    returncode: int, output: str, kind: ResticOperationalKind | None
) -> None:
    assert classify_restore_operational(returncode=returncode, output=output) is kind
    if kind is not None:
        assert "Unable to complete restore verification" in restore_error_message(kind)


def test_version_string_stable() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert f"backuplint {__version__}" in result.stdout
