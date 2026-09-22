"""Human and machine-readable BackupLint audit reports."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum

from backuplint.config import IntegrityMode
from backuplint.coverage import CoverageFinding, CoverageStatus
from backuplint.restic import IntegrityCheckResult, IntegrityStatus
from backuplint.restore_verify import (
    RestoreVerificationResult,
    RestoreVerificationStatus,
)


class AuditResult(StrEnum):
    PASS = "PASS"  # noqa: S105  # nosec B105 — audit outcome label, not a password
    WARN = "WARN"
    FAIL = "FAIL"
    ERROR = "ERROR"


@dataclass(frozen=True)
class AuditSummary:
    protected: int
    warning: int
    critical: int
    skipped: int
    result: AuditResult

    @property
    def exit_code(self) -> int:
        # 0 = no critical problems (PASS or WARN), 1 = coverage/integrity failures,
        # 2 = operational ERROR (repo unavailable, auth, runtime).
        if self.result is AuditResult.ERROR:
            return 2
        if self.result is AuditResult.FAIL:
            return 1
        return 0


def summarize_findings(
    findings: list[CoverageFinding],
    *,
    integrity: IntegrityCheckResult | None = None,
    restore: RestoreVerificationResult | None = None,
) -> AuditSummary:
    protected = 0
    warning = 0
    critical = 0
    skipped = 0
    for finding in findings:
        if finding.status is CoverageStatus.PROTECTED:
            protected += 1
        elif finding.status is CoverageStatus.NOT_PROTECTED:
            critical += 1
        elif finding.status is CoverageStatus.SKIPPED:
            skipped += 1
        else:
            # UNSUPPORTED and STALE are warnings.
            warning += 1

    if critical:
        result = AuditResult.FAIL
    elif warning:
        result = AuditResult.WARN
    else:
        result = AuditResult.PASS

    if integrity is not None:
        if integrity.status is IntegrityStatus.FAILED:
            result = AuditResult.FAIL
        elif integrity.status is IntegrityStatus.STALE and result is AuditResult.PASS:
            result = AuditResult.WARN

    if restore is not None:
        if restore.status is RestoreVerificationStatus.FAILED:
            result = AuditResult.FAIL
        elif restore.status is RestoreVerificationStatus.STALE and result is AuditResult.PASS:
            result = AuditResult.WARN
        elif restore.status is RestoreVerificationStatus.SKIPPED and result is AuditResult.PASS:
            result = AuditResult.WARN

    return AuditSummary(
        protected=protected,
        warning=warning,
        critical=critical,
        skipped=skipped,
        result=result,
    )


def _status_label(status: CoverageStatus) -> str:
    return {
        CoverageStatus.PROTECTED: "protected",
        CoverageStatus.NOT_PROTECTED: "not protected",
        CoverageStatus.SKIPPED: "skipped",
        CoverageStatus.UNSUPPORTED: "review required",
        CoverageStatus.STALE: "stale",
    }[status]


def _mark(status: CoverageStatus) -> str:
    return {
        CoverageStatus.PROTECTED: "✓",
        CoverageStatus.NOT_PROTECTED: "✗",
        CoverageStatus.SKIPPED: "·",
        CoverageStatus.UNSUPPORTED: "⚠",
        CoverageStatus.STALE: "⚠",
    }[status]


def _finding_label(finding: CoverageFinding) -> str:
    if finding.host_path:
        return finding.host_path
    if finding.mount is not None:
        return finding.mount.source or finding.mount.target
    return "database detected"


def _format_duration(seconds: float) -> str:
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{seconds:.0f}s"


def _format_integrity_section(integrity: IntegrityCheckResult) -> list[str]:
    if integrity.status is IntegrityStatus.NOT_REQUESTED:
        return []
    lines = ["", "Restic integrity"]
    mode = integrity.mode.value
    duration = _format_duration(integrity.duration_seconds)
    if integrity.status is IntegrityStatus.PASSED:
        if integrity.mode is IntegrityMode.DEEP:
            label = "deep data integrity check passed"
        else:
            label = "standard integrity check passed"
        lines.append(f"✓ {label}        {mode}        {duration}")
    elif integrity.status is IntegrityStatus.FAILED:
        kind = (
            "deep data integrity check failed"
            if integrity.mode is IntegrityMode.DEEP
            else "standard integrity check failed"
        )
        lines.append(f"✗ {kind}        repository inconsistency detected")
    elif integrity.status is IntegrityStatus.STALE:
        lines.append(f"⚠ integrity verification stale        {integrity.message}")
    else:
        lines.append(f"✗ repository check error        {integrity.message}")
    return lines


def _format_restore_section(restore: RestoreVerificationResult) -> list[str]:
    if restore.status is RestoreVerificationStatus.NOT_REQUESTED:
        return []
    lines = ["", "Restic restore verification"]
    mode = restore.mode.value
    duration = _format_duration(restore.duration_seconds)
    if restore.status is RestoreVerificationStatus.PASSED:
        lines.append(f"✓ {restore.message}        {mode}        {duration}")
    elif restore.status is RestoreVerificationStatus.FAILED:
        lines.append(f"✗ restore verification failed        {restore.message}")
    elif restore.status is RestoreVerificationStatus.SKIPPED:
        lines.append(f"⚠ restore verification skipped        {restore.message}")
    elif restore.status is RestoreVerificationStatus.STALE:
        lines.append(f"⚠ restore verification stale        {restore.message}")
    else:
        lines.append(f"✗ restore verification error        {restore.message}")
    return lines


def format_audit_report(
    findings: list[CoverageFinding],
    *,
    integrity: IntegrityCheckResult | None = None,
    restore: RestoreVerificationResult | None = None,
) -> str:
    """Render a PASS / WARN / FAIL audit report."""
    summary = summarize_findings(findings, integrity=integrity, restore=restore)
    if not findings:
        lines = [
            "BackupLint Audit",
            "",
            "No mounts found to evaluate.",
            *_format_integrity_section(
                integrity
                or IntegrityCheckResult(
                    mode=IntegrityMode.OFF,
                    status=IntegrityStatus.NOT_REQUESTED,
                    duration_seconds=0.0,
                    message="",
                )
            ),
            *(
                _format_restore_section(restore)
                if restore is not None
                else []
            ),
            "",
            f"Result: {summary.result.value}",
            "",
            "Summary:",
            "0 protected",
            "0 warning",
            "0 critical",
        ]
        return "\n".join(lines)

    service_width = max(len(f.service) for f in findings)
    path_width = max(len(_finding_label(f)) for f in findings)

    lines = ["BackupLint Audit", ""]
    for finding in findings:
        label = _finding_label(finding)
        lines.append(
            f"{_mark(finding.status)} {finding.service:<{service_width}}  "
            f"{label:<{path_width}}  {_status_label(finding.status)}"
        )
        if finding.status is CoverageStatus.PROTECTED and finding.detail:
            lines.append(f"  {finding.detail}")
        elif finding.status in {
            CoverageStatus.UNSUPPORTED,
            CoverageStatus.STALE,
            CoverageStatus.NOT_PROTECTED,
        }:
            lines.append(f"  {finding.detail}")

    if integrity is not None:
        lines.extend(_format_integrity_section(integrity))
    if restore is not None:
        lines.extend(_format_restore_section(restore))

    lines.extend(
        [
            "",
            f"Result: {summary.result.value}",
            "",
            "Summary:",
            f"{summary.protected} protected",
            f"{summary.warning} warning",
            f"{summary.critical} critical",
        ]
    )
    if summary.skipped:
        lines.append(f"{summary.skipped} skipped")
    return "\n".join(lines)


def _integrity_json(integrity: IntegrityCheckResult | None) -> dict[str, object] | None:
    if integrity is None or integrity.status is IntegrityStatus.NOT_REQUESTED:
        return None
    payload: dict[str, object] = {
        "requested": integrity.requested,
        "mode": integrity.mode.value,
        "status": integrity.status.value,
        "duration_seconds": round(integrity.duration_seconds, 3),
        "message": integrity.message,
    }
    if integrity.checked_at is not None:
        payload["checked_at"] = (
            integrity.checked_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        )
    if integrity.restic_version is not None:
        payload["restic_version"] = integrity.restic_version
    return payload


def _restore_json(restore: RestoreVerificationResult | None) -> dict[str, object] | None:
    if restore is None or restore.status is RestoreVerificationStatus.NOT_REQUESTED:
        return None
    payload: dict[str, object] = {
        "requested": restore.requested,
        "mode": restore.mode.value,
        "status": restore.status.value,
        "duration_seconds": round(restore.duration_seconds, 3),
        "message": restore.message,
        "paths_requested": restore.paths_requested,
        "paths_restored": restore.paths_restored,
        "files_restored": restore.files_restored,
        "bytes_restored": restore.bytes_restored,
    }
    if restore.snapshot_id is not None:
        payload["snapshot_id"] = restore.snapshot_id
    if restore.validation is not None:
        payload["validation"] = {
            "status": restore.validation.status.value,
            "message": restore.validation.message,
            "files_checked": restore.validation.files_checked,
            "bytes_checked": restore.validation.bytes_checked,
        }
    if restore.checked_at is not None:
        payload["checked_at"] = (
            restore.checked_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        )
    if restore.restic_version is not None:
        payload["restic_version"] = restore.restic_version
    return payload


def format_audit_json(
    findings: list[CoverageFinding],
    *,
    integrity: IntegrityCheckResult | None = None,
    restore: RestoreVerificationResult | None = None,
) -> str:
    """Render a simple machine-readable audit payload."""
    summary = summarize_findings(findings, integrity=integrity, restore=restore)
    payload: dict[str, object] = {
        "result": summary.result.value,
        "summary": {
            "protected": summary.protected,
            "warning": summary.warning,
            "critical": summary.critical,
            "skipped": summary.skipped,
        },
        "findings": [
            {
                "service": f.service,
                "status": f.status.value,
                "label": _status_label(f.status),
                "path": f.host_path
                or (f.mount.source if f.mount else None)
                or (f.mount.target if f.mount else None),
                "target": f.mount.target if f.mount else None,
                "type": f.mount.type.value if f.mount else None,
                "storage_class": f.storage_class.value if f.storage_class else None,
                "detail": f.detail,
                "covered_by": f.covered_by,
                "critical": f.is_critical,
                "project": f.project,
                "image": f.image,
                "snapshot_time": (
                    (
                        f.snapshot_time
                        if f.snapshot_time.tzinfo is not None
                        else f.snapshot_time.replace(tzinfo=UTC)
                    )
                    .astimezone(UTC)
                    .isoformat()
                    .replace("+00:00", "Z")
                    if f.snapshot_time is not None
                    else None
                ),
            }
            for f in findings
        ],
    }
    integrity_payload = _integrity_json(integrity)
    if integrity_payload is not None:
        payload["integrity"] = integrity_payload
    restore_payload = _restore_json(restore)
    if restore_payload is not None:
        payload["restore_verification"] = restore_payload
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def format_operational_error_json(
    *,
    message: str,
    kind: str,
    engine: str = "restic",
) -> str:
    """Machine-readable operational ERROR (not coverage FAIL / integrity FAILED)."""
    payload: dict[str, object] = {
        "result": AuditResult.ERROR.value,
        "summary": {
            "protected": 0,
            "warning": 0,
            "critical": 0,
            "skipped": 0,
        },
        "findings": [],
        "operational_error": {
            "engine": engine,
            "kind": kind,
            "message": message,
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def format_coverage_report(findings: list[CoverageFinding]) -> str:
    """Compatibility alias for coverage-only audit text output."""
    return format_audit_report(findings)
