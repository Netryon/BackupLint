"""Unit tests for the backup-engine adapter contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backuplint.config import ResticConfig
from backuplint.engine import (
    BackupEngineError,
    OperationalKind,
    ResticBackupEngine,
    classify_engine_exception,
    get_backup_engine,
)
from backuplint.reporting import format_operational_error_json
from backuplint.restic import ResticError


def test_get_backup_engine_returns_restic() -> None:
    eng = get_backup_engine(
        ResticConfig(repository="/tmp/no-such-repo", password_file=Path("/tmp/pw"))
    )
    assert eng.engine_id == "restic"
    caps = eng.capabilities()
    assert caps.list_snapshots is True
    assert caps.standard_integrity is True
    assert caps.deep_integrity is True


def test_classify_inaccessible_repository() -> None:
    err = classify_engine_exception(
        ResticError("Unable to open Restic repository (inaccessible or missing).")
    )
    assert err.kind is OperationalKind.REPOSITORY_UNAVAILABLE
    assert err.engine_id == "restic"


def test_classify_auth_failure() -> None:
    err = classify_engine_exception(
        ResticError("Unable to open Restic repository (authentication failed).")
    )
    assert err.kind is OperationalKind.AUTHENTICATION


def test_operational_error_json_not_coverage_fail() -> None:
    payload = json.loads(
        format_operational_error_json(
            message="Unable to open Restic repository (inaccessible or missing).",
            kind=OperationalKind.REPOSITORY_UNAVAILABLE.value,
            engine="restic",
        )
    )
    assert payload["result"] == "ERROR"
    assert payload["summary"]["critical"] == 0
    assert payload["findings"] == []
    assert payload["operational_error"]["kind"] == "repository_unavailable"
    assert "FAIL" not in payload["result"]


def test_restic_engine_list_snapshots_wraps_missing_repo(tmp_path: Path) -> None:
    pw = tmp_path / "pw"
    pw.write_text("x\n", encoding="utf-8")
    eng = ResticBackupEngine(
        ResticConfig(repository=str(tmp_path / "missing-repo"), password_file=pw)
    )
    with pytest.raises(BackupEngineError) as ei:
        eng.list_snapshots()
    assert ei.value.kind in {
        OperationalKind.REPOSITORY_UNAVAILABLE,
        OperationalKind.RUNTIME,
        OperationalKind.AUTHENTICATION,
    }
    status = eng.repository_status()
    assert status.available is False
    assert status.kind is not None
