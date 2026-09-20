"""Deterministic PASS/WARN/FAIL/ERROR composition for coverage + integrity."""

from __future__ import annotations

from backuplint.classify import StorageClass
from backuplint.config import IntegrityMode
from backuplint.coverage import CoverageFinding, CoverageStatus, evaluate_mount
from backuplint.models import Mount, MountType
from backuplint.reporting import AuditResult, summarize_findings
from backuplint.restic import IntegrityCheckResult, IntegrityStatus


def _bind(source: str) -> Mount:
    return Mount(service="web", type=MountType.BIND, source=source, target="/data")


def _protected() -> list[CoverageFinding]:
    return [evaluate_mount(_bind("/srv/data"), ("/srv/data",))]


def _missing() -> list[CoverageFinding]:
    return [evaluate_mount(_bind("/srv/miss"), ("/srv/other",))]


def _stale_coverage() -> list[CoverageFinding]:
    return [
        CoverageFinding(
            service="web",
            mount=_bind("/srv/data"),
            storage_class=StorageClass.PERSISTENT,
            status=CoverageStatus.STALE,
            detail="latest relevant backup is stale",
            host_path="/srv/data",
        )
    ]


def _integrity(
    status: IntegrityStatus,
    mode: IntegrityMode = IntegrityMode.STANDARD,
) -> IntegrityCheckResult:
    return IntegrityCheckResult(
        mode=mode,
        status=status,
        duration_seconds=1.0,
        message=status.value,
        requested=status is not IntegrityStatus.NOT_REQUESTED,
    )


def test_precedence_truth_table() -> None:
    rows = [
        (_protected(), _integrity(IntegrityStatus.PASSED), AuditResult.PASS, 0),
        (_stale_coverage(), _integrity(IntegrityStatus.PASSED), AuditResult.WARN, 0),
        (_missing(), _integrity(IntegrityStatus.PASSED), AuditResult.FAIL, 1),
        (_protected(), _integrity(IntegrityStatus.FAILED), AuditResult.FAIL, 1),
        (_missing(), _integrity(IntegrityStatus.FAILED), AuditResult.FAIL, 1),
        (_protected(), _integrity(IntegrityStatus.STALE), AuditResult.WARN, 0),
        (_missing(), _integrity(IntegrityStatus.STALE), AuditResult.FAIL, 1),
        (_protected(), _integrity(IntegrityStatus.NOT_REQUESTED), AuditResult.PASS, 0),
        (
            _protected(),
            _integrity(IntegrityStatus.PASSED, mode=IntegrityMode.DEEP),
            AuditResult.PASS,
            0,
        ),
    ]
    for findings, integrity, expected, exit_code in rows:
        summary = summarize_findings(findings, integrity=integrity)
        assert summary.result is expected
        assert summary.exit_code == exit_code
