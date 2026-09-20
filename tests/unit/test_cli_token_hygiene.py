"""CLI enrollment token hygiene using SecretRef resolution."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from backuplint.cli import app

runner = CliRunner()


def test_agent_enroll_requires_exactly_one_token_source() -> None:
    result = runner.invoke(
        app,
        [
            "agent",
            "enroll",
            "--controller",
            "https://127.0.0.1:1",
            "--ca-cert",
            "/tmp/no-ca.pem",
            "--agent-id",
            "agent-clihygiene01",
        ],
    )
    assert result.exit_code != 0
    assert "exactly one" in (result.stdout + result.stderr).lower() or "exactly one" in str(
        result.exception or ""
    ) or "Provide exactly one" in (result.output or "")


def test_agent_enroll_token_file_permission_error(tmp_path: Path) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("enrollment-token-value\n", encoding="utf-8")
    token_path.chmod(0o644)
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    result = runner.invoke(
        app,
        [
            "agent",
            "enroll",
            "--controller",
            "https://127.0.0.1:1",
            "--ca-cert",
            str(ca),
            "--token-file",
            str(token_path),
            "--agent-id",
            "agent-clihygiene02",
        ],
    )
    assert result.exit_code != 0
    combined = (result.output or "") + str(result.exception or "")
    assert "enrollment-token-value" not in combined
    assert "world-readable" in combined or "secret file" in combined
