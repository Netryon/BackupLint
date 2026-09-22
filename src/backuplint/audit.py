"""Application orchestration for a BackupLint scan (CLI-independent)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backuplint.compose import discover_mounts
from backuplint.config import (
    BackupLintConfig,
    ConfigError,
    IntegrityConfig,
    IntegrityMode,
    RestoreVerificationMode,
    default_config_path,
    load_config,
    parse_integrity_mode,
    parse_restore_verification_mode,
)
from backuplint.coverage import (
    CoverageFinding,
    CoverageStatus,
    evaluate_coverage,
    evaluate_coverage_restic,
)
from backuplint.engine import BackupEngineError, get_backup_engine
from backuplint.integrity_state import (
    IntegrityStateError,
    is_integrity_success_stale,
    last_success_for_repository,
    load_integrity_state,
    record_integrity_success,
)
from backuplint.models import ServiceMounts
from backuplint.reporting import AuditSummary, summarize_findings
from backuplint.restic import (
    IntegrityCheckResult,
    IntegrityStatus,
    ResticError,
)
from backuplint.restore_verify import (
    RestoreVerificationResult,
    RestoreVerificationStatus,
    verify_restore,
)


@dataclass(frozen=True)
class AuditOutcome:
    """Result of a full audit orchestration pass."""

    findings: list[CoverageFinding]
    summary: AuditSummary
    integrity: IntegrityCheckResult | None
    restore: RestoreVerificationResult | None


def resolve_integrity_mode(
    *,
    configured: IntegrityMode,
    cli_override: str | None,
    restic_configured: bool,
) -> IntegrityMode:
    """CLI option overrides config; config overrides the safe default (off)."""
    if cli_override is not None:
        mode = parse_integrity_mode(cli_override)
    else:
        mode = configured
    if mode is not IntegrityMode.OFF and not restic_configured:
        raise ConfigError(
            "Integrity checking requires a 'restic' configuration section."
        )
    return mode


def resolve_restore_mode(
    *,
    configured: RestoreVerificationMode,
    cli_override: str | None,
    restic_configured: bool,
) -> RestoreVerificationMode:
    """CLI option overrides config; config overrides the safe default (off)."""
    if cli_override is not None:
        mode = parse_restore_verification_mode(cli_override)
    else:
        mode = configured
    if mode is not RestoreVerificationMode.OFF and not restic_configured:
        raise ConfigError(
            "Restore verification requires a 'restic' configuration section."
        )
    return mode


def relevant_restore_paths(findings: list[CoverageFinding]) -> tuple[str, ...]:
    """Host paths eligible for restore verification (historical selection rules)."""
    paths: list[str] = []
    seen: set[str] = set()
    for finding in findings:
        if finding.status not in {CoverageStatus.PROTECTED, CoverageStatus.STALE}:
            continue
        if not finding.host_path:
            continue
        if finding.host_path in seen:
            continue
        seen.add(finding.host_path)
        paths.append(finding.host_path)
    return tuple(paths)


def stale_integrity_from_state(
    *,
    repository: str,
    integrity_cfg: IntegrityConfig,
) -> IntegrityCheckResult | None:
    """When live checks are off, optionally WARN from remembered success age."""
    if integrity_cfg.max_age is None or integrity_cfg.state_file is None:
        return None
    try:
        records = load_integrity_state(integrity_cfg.state_file)
    except IntegrityStateError as exc:
        raise ResticError(exc.message) from exc
    record = last_success_for_repository(records, repository)
    if is_integrity_success_stale(record, max_age=integrity_cfg.max_age):
        return IntegrityCheckResult(
            mode=IntegrityMode.OFF,
            status=IntegrityStatus.STALE,
            duration_seconds=0.0,
            message=(
                "last successful integrity verification is older than configured max_age"
            ),
            requested=False,
        )
    return IntegrityCheckResult(
        mode=record.mode if record is not None else IntegrityMode.OFF,
        status=IntegrityStatus.NOT_REQUESTED,
        duration_seconds=0.0,
        message="Integrity verification was not requested.",
        requested=False,
    )


def run_audit(
    compose_file: Path,
    *,
    config_path: Path | None = None,
    integrity_cli: str | None = None,
    restore_verify_cli: str | None = None,
) -> AuditOutcome:
    """Discover mounts, evaluate coverage/integrity/restore, return summary."""
    resolved_config = (
        config_path if config_path is not None else default_config_path(compose_file)
    )
    backup_config = load_config(resolved_config)
    services = discover_mounts(
        compose_file, extra_files=backup_config.compose_files
    )
    return run_audit_with_config(
        services=services,
        backup_config=backup_config,
        integrity_cli=integrity_cli,
        restore_verify_cli=restore_verify_cli,
    )


def run_audit_with_config(
    *,
    services: list[ServiceMounts],
    backup_config: BackupLintConfig,
    integrity_cli: str | None = None,
    restore_verify_cli: str | None = None,
) -> AuditOutcome:
    configured_mode = (
        backup_config.restic.integrity.mode
        if backup_config.restic is not None
        else IntegrityMode.OFF
    )
    integrity_mode = resolve_integrity_mode(
        configured=configured_mode,
        cli_override=integrity_cli,
        restic_configured=backup_config.restic is not None,
    )
    configured_restore = (
        backup_config.restic.restore_verification.mode
        if backup_config.restic is not None
        else RestoreVerificationMode.OFF
    )
    restore_mode = resolve_restore_mode(
        configured=configured_restore,
        cli_override=restore_verify_cli,
        restic_configured=backup_config.restic is not None,
    )

    integrity_result: IntegrityCheckResult | None = None
    restore_result: RestoreVerificationResult | None = None

    if backup_config.restic is not None:
        engine = get_backup_engine(backup_config.restic)
        try:
            snapshots = engine.list_snapshots()
        except BackupEngineError as exc:
            # Preserve historical CLI/scheduler raise semantics (ResticError).
            raise ResticError(exc.message) from exc
        findings = evaluate_coverage_restic(
            services,
            snapshots,
            max_backup_age=backup_config.max_backup_age,
        )
        integrity_cfg = backup_config.restic.integrity
        if integrity_mode is not IntegrityMode.OFF:
            # Explicit live check — never silently downgrade deep to standard.
            try:
                integrity_result = engine.integrity_check(integrity_mode)
            except BackupEngineError as exc:
                raise ResticError(exc.message) from exc
            if integrity_result.status is IntegrityStatus.ERROR:
                raise ResticError(integrity_result.message)
            if (
                integrity_result.status is IntegrityStatus.PASSED
                and integrity_cfg.state_file is not None
            ):
                try:
                    record_integrity_success(
                        integrity_cfg.state_file,
                        repository=backup_config.restic.repository,
                        mode=integrity_result.mode,
                        checked_at=integrity_result.checked_at,
                        restic_version=integrity_result.restic_version,
                    )
                except IntegrityStateError as exc:
                    raise ResticError(exc.message) from exc
        else:
            integrity_result = stale_integrity_from_state(
                repository=backup_config.restic.repository,
                integrity_cfg=integrity_cfg,
            )
            if integrity_result is None:
                integrity_result = IntegrityCheckResult(
                    mode=IntegrityMode.OFF,
                    status=IntegrityStatus.NOT_REQUESTED,
                    duration_seconds=0.0,
                    message="Integrity verification was not requested.",
                    requested=False,
                )

        restore_cfg = backup_config.restic.restore_verification
        if restore_mode is not RestoreVerificationMode.OFF:
            paths = relevant_restore_paths(findings)
            live_binds = tuple(Path(p) for p in paths)
            timeout_seconds = (
                restore_cfg.timeout.total_seconds()
                if restore_cfg.timeout is not None
                else None
            )
            restore_result = verify_restore(
                mode=restore_mode,
                repository=backup_config.restic.repository,
                snapshots=snapshots,
                relevant_paths=paths,
                password_file=backup_config.restic.password_file,
                password_ref=backup_config.restic.password,
                timeout=timeout_seconds,
                expected_paths=restore_cfg.expected_paths,
                live_bind_paths=live_binds,
            )
            if restore_result.status is RestoreVerificationStatus.ERROR:
                raise ResticError(restore_result.message)
        else:
            restore_result = RestoreVerificationResult(
                mode=RestoreVerificationMode.OFF,
                status=RestoreVerificationStatus.NOT_REQUESTED,
                duration_seconds=0.0,
                message="Restore verification was not requested.",
                requested=False,
            )
    else:
        findings = evaluate_coverage(services, backup_config.backup_paths)
        restore_result = RestoreVerificationResult(
            mode=RestoreVerificationMode.OFF,
            status=RestoreVerificationStatus.NOT_REQUESTED,
            duration_seconds=0.0,
            message="Restore verification was not requested.",
            requested=False,
        )

    summary = summarize_findings(
        findings, integrity=integrity_result, restore=restore_result
    )
    return AuditOutcome(
        findings=findings,
        summary=summary,
        integrity=integrity_result,
        restore=restore_result,
    )
