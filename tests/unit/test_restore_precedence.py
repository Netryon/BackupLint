"""Unit tests for restore verification reporting/precedence."""

from __future__ import annotations

from backuplint.config import IntegrityMode, RestoreVerificationMode
from backuplint.coverage import CoverageFinding, CoverageStatus
from backuplint.reporting import AuditResult, summarize_findings
from backuplint.restic import IntegrityCheckResult, IntegrityStatus
from backuplint.restore_verify import (
    RestoreVerificationResult,
    RestoreVerificationStatus,
)


def _finding(status: CoverageStatus) -> CoverageFinding:
    return CoverageFinding(
        service="app",
        mount=None,
        storage_class=None,
        status=status,
        detail="x",
        host_path="/data",
    )


def test_restore_failed_forces_fail() -> None:
    summary = summarize_findings(
        [_finding(CoverageStatus.PROTECTED)],
        integrity=IntegrityCheckResult(
            mode=IntegrityMode.STANDARD,
            status=IntegrityStatus.PASSED,
            duration_seconds=1.0,
            message="ok",
        ),
        restore=RestoreVerificationResult(
            mode=RestoreVerificationMode.SELECTED,
            status=RestoreVerificationStatus.FAILED,
            duration_seconds=1.0,
            message="restore verification failed",
        ),
    )
    assert summary.result is AuditResult.FAIL
    assert summary.exit_code == 1


def test_restore_pass_does_not_worsen_pass() -> None:
    summary = summarize_findings(
        [_finding(CoverageStatus.PROTECTED)],
        restore=RestoreVerificationResult(
            mode=RestoreVerificationMode.SELECTED,
            status=RestoreVerificationStatus.PASSED,
            duration_seconds=1.0,
            message="selected-path restore verification passed",
        ),
    )
    assert summary.result is AuditResult.PASS
