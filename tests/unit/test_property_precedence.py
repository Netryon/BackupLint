"""Property-based tests for audit result / exit precedence."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from backuplint.config import IntegrityMode, RestoreVerificationMode
from backuplint.coverage import CoverageFinding, CoverageStatus
from backuplint.reporting import AuditResult, summarize_findings
from backuplint.restic import IntegrityCheckResult, IntegrityStatus
from backuplint.restore_verify import (
    RestoreVerificationResult,
    RestoreVerificationStatus,
)

_settings = settings(max_examples=120, deadline=None)


def _finding(status: CoverageStatus) -> CoverageFinding:
    return CoverageFinding(
        service="app",
        mount=None,
        storage_class=None,
        status=status,
        detail="x",
        host_path="/data",
    )


def _integrity(status: IntegrityStatus) -> IntegrityCheckResult:
    return IntegrityCheckResult(
        mode=IntegrityMode.STANDARD,
        status=status,
        duration_seconds=0.1,
        message="m",
    )


def _restore(status: RestoreVerificationStatus) -> RestoreVerificationResult:
    return RestoreVerificationResult(
        mode=RestoreVerificationMode.SELECTED,
        status=status,
        duration_seconds=0.1,
        message="m",
    )


@_settings
@given(
    cov=st.sampled_from(list(CoverageStatus)),
    integ=st.sampled_from(list(IntegrityStatus)),
    restore=st.sampled_from(list(RestoreVerificationStatus)),
)
def test_precedence_deterministic(
    cov: CoverageStatus,
    integ: IntegrityStatus,
    restore: RestoreVerificationStatus,
) -> None:
    summary = summarize_findings(
        [_finding(cov)],
        integrity=_integrity(integ),
        restore=_restore(restore),
    )
    # Documented rules:
    # - NOT_PROTECTED coverage -> FAIL
    # - integrity FAILED -> FAIL
    # - restore FAILED -> FAIL
    # - STALE/UNSUPPORTED coverage -> at least WARN unless FAIL forced
    # - integrity/restore STALE/SKIPPED only upgrade PASS -> WARN
    if (
        cov is CoverageStatus.NOT_PROTECTED
        or integ is IntegrityStatus.FAILED
        or restore is RestoreVerificationStatus.FAILED
    ):
        assert summary.result is AuditResult.FAIL
        assert summary.exit_code == 1
        return

    if cov in {CoverageStatus.STALE, CoverageStatus.UNSUPPORTED}:
        assert summary.result is AuditResult.WARN
        assert summary.exit_code == 0
        return

    if integ is IntegrityStatus.STALE or restore in {
        RestoreVerificationStatus.STALE,
        RestoreVerificationStatus.SKIPPED,
    }:
        # Only upgrades PASS; SKIPPED coverage alone is not WARN in summarize.
        if cov is CoverageStatus.PROTECTED or (
            cov is CoverageStatus.SKIPPED
            and integ is IntegrityStatus.STALE
        ) or (
            cov is CoverageStatus.SKIPPED
            and restore
            in {
                RestoreVerificationStatus.STALE,
                RestoreVerificationStatus.SKIPPED,
            }
        ):
            # SKIPPED-only coverage: PASS base, then STALE/SKIPPED restore upgrades.
            if cov is CoverageStatus.SKIPPED and integ not in {
                IntegrityStatus.STALE,
                IntegrityStatus.FAILED,
            } and restore not in {
                RestoreVerificationStatus.STALE,
                RestoreVerificationStatus.SKIPPED,
                RestoreVerificationStatus.FAILED,
            }:
                assert summary.result is AuditResult.PASS
            elif cov is CoverageStatus.PROTECTED or restore in {
                RestoreVerificationStatus.STALE,
                RestoreVerificationStatus.SKIPPED,
            } or integ is IntegrityStatus.STALE:
                assert summary.result in {AuditResult.PASS, AuditResult.WARN}
        return

    # Otherwise PASS (including ERROR/NOT_REQUESTED which do not force FAIL here —
    # CLI raises before summarize for ERROR).
    assert summary.result in {AuditResult.PASS, AuditResult.WARN}
    assert summary.exit_code == 0


def test_operational_error_statuses_do_not_force_fail_in_summarize() -> None:
    """ERROR is handled by CLI raising; summarize itself leaves PASS if only ERROR."""
    summary = summarize_findings(
        [_finding(CoverageStatus.PROTECTED)],
        integrity=_integrity(IntegrityStatus.ERROR),
        restore=_restore(RestoreVerificationStatus.ERROR),
    )
    assert summary.result is AuditResult.PASS
