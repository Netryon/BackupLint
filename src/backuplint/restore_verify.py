"""High-level Restic restore verification orchestration."""

from __future__ import annotations

import os
import signal
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from backuplint.config import RestoreVerificationMode
from backuplint.restic import (
    ResticError,
    ResticSnapshot,
    latest_relevant_snapshot,
    probe_restic_version,
    restore_snapshot_paths,
)
from backuplint.restic_errors import (
    classify_restore_operational,
    restore_error_message,
)
from backuplint.restore_dest import (
    RestoreDestinationError,
    cleanup_owned_restore_root,
    create_owned_restore_root,
    expected_materialized_path,
)


class RestoreVerificationStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    STALE = "stale"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class RestoreValidationResult:
    status: RestoreVerificationStatus
    message: str
    files_checked: int = 0
    bytes_checked: int = 0


@dataclass(frozen=True)
class RestoreVerificationResult:
    mode: RestoreVerificationMode
    status: RestoreVerificationStatus
    duration_seconds: float
    message: str
    snapshot_id: str | None = None
    paths_requested: int = 0
    paths_restored: int = 0
    files_restored: int = 0
    bytes_restored: int = 0
    destination: str | None = None
    validation: RestoreValidationResult | None = None
    checked_at: datetime | None = None
    restic_version: str | None = None
    requested: bool = True


def _classify_restore_operational_failure(
    *,
    returncode: int,
    output: str,
) -> str | None:
    """Return an ERROR message for operational failures, else None."""
    kind = classify_restore_operational(returncode=returncode, output=output or "")
    if kind is None:
        return None
    return restore_error_message(kind)


def validate_restored_paths(
    *,
    restore_root: Path,
    source_paths: tuple[str, ...],
    expected_relative_paths: tuple[str, ...] = (),
    min_files: int = 1,
) -> RestoreValidationResult:
    """Validate that restored content exists and is readable under the target."""
    files_checked = 0
    bytes_checked = 0
    missing: list[str] = []

    for source in source_paths:
        materialized = expected_materialized_path(restore_root, source)
        if not materialized.exists():
            missing.append(source)
            continue
        if materialized.is_file():
            try:
                data = materialized.read_bytes()
            except OSError:
                return RestoreValidationResult(
                    status=RestoreVerificationStatus.FAILED,
                    message=f"Restored file is not readable: {source}",
                )
            files_checked += 1
            bytes_checked += len(data)
            continue
        if not materialized.is_dir():
            missing.append(source)
            continue
        for dirpath, _dirnames, filenames in os.walk(materialized):
            for name in filenames:
                path = Path(dirpath) / name
                if path.name.startswith(".backuplint-"):
                    continue
                # Restored broken symlinks are valid tree members; do not require
                # the link target to exist on the restore host.
                if path.is_symlink():
                    try:
                        path.readlink()
                    except OSError:
                        return RestoreValidationResult(
                            status=RestoreVerificationStatus.FAILED,
                            message=f"Restored symlink is not readable under {source}",
                        )
                    files_checked += 1
                    continue
                try:
                    with path.open("rb") as handle:
                        chunk = handle.read(65536)
                except OSError:
                    return RestoreValidationResult(
                        status=RestoreVerificationStatus.FAILED,
                        message=f"Restored file is not readable under {source}",
                    )
                files_checked += 1
                try:
                    bytes_checked += path.stat().st_size
                except OSError:
                    bytes_checked += len(chunk)

    if missing:
        return RestoreValidationResult(
            status=RestoreVerificationStatus.FAILED,
            message="Restore verification failed: expected restored path(s) missing.",
            files_checked=files_checked,
            bytes_checked=bytes_checked,
        )

    for rel in expected_relative_paths:
        found = False
        for source in source_paths:
            candidate = expected_materialized_path(restore_root, source) / rel
            if candidate.exists():
                found = True
                break
        if not found:
            found = (restore_root / rel).exists()
        if not found:
            return RestoreValidationResult(
                status=RestoreVerificationStatus.FAILED,
                message=f"Restore verification failed: expected path missing: {rel}",
                files_checked=files_checked,
                bytes_checked=bytes_checked,
            )

    if files_checked < min_files:
        return RestoreValidationResult(
            status=RestoreVerificationStatus.FAILED,
            message="Restore verification failed: restored content had too few files.",
            files_checked=files_checked,
            bytes_checked=bytes_checked,
        )

    return RestoreValidationResult(
        status=RestoreVerificationStatus.PASSED,
        message="restored content validation passed",
        files_checked=files_checked,
        bytes_checked=bytes_checked,
    )


def _pick_shared_snapshot(
    snapshots: list[ResticSnapshot],
    paths: tuple[str, ...],
) -> ResticSnapshot | None:
    if not paths:
        return None
    primary = latest_relevant_snapshot(snapshots, paths[0])
    if primary is None:
        return None
    for path in paths[1:]:
        if latest_relevant_snapshot([primary], path) is None:
            return None
    return primary


def verify_restore(
    *,
    mode: RestoreVerificationMode,
    repository: str,
    snapshots: list[ResticSnapshot],
    relevant_paths: tuple[str, ...],
    password: str | None = None,
    password_file: Path | None = None,
    password_ref: object | None = None,
    timeout: float | None = None,
    expected_paths: tuple[str, ...] = (),
    live_bind_paths: tuple[Path, ...] = (),
    live_volume_mountpoints: tuple[Path, ...] = (),
) -> RestoreVerificationResult:
    """Perform an isolated real Restic restore and validate restored content."""
    checked_at = datetime.now(UTC)
    if mode is RestoreVerificationMode.OFF:
        return RestoreVerificationResult(
            mode=mode,
            status=RestoreVerificationStatus.NOT_REQUESTED,
            duration_seconds=0.0,
            message="Restore verification was not requested.",
            requested=False,
        )

    paths = relevant_paths
    if not paths:
        return RestoreVerificationResult(
            mode=mode,
            status=RestoreVerificationStatus.SKIPPED,
            duration_seconds=0.0,
            message="Restore verification skipped: no relevant backup paths to restore.",
            checked_at=checked_at,
            restic_version=probe_restic_version(),
            requested=True,
        )

    shared = _pick_shared_snapshot(snapshots, paths)
    if shared is not None:
        per_path = [(path, shared) for path in paths]
    else:
        per_path = []
        for path in paths:
            snap = latest_relevant_snapshot(snapshots, path)
            if snap is None:
                return RestoreVerificationResult(
                    mode=mode,
                    status=RestoreVerificationStatus.FAILED,
                    duration_seconds=0.0,
                    message=(
                        "Restore verification failed: no relevant snapshot covers "
                        f"path {path}."
                    ),
                    paths_requested=len(paths),
                    checked_at=checked_at,
                    restic_version=probe_restic_version(),
                )
            per_path.append((path, snap))

    try:
        owned = create_owned_restore_root(
            repository=repository,
            live_bind_paths=live_bind_paths,
            live_volume_mountpoints=live_volume_mountpoints,
        )
    except RestoreDestinationError as exc:
        return RestoreVerificationResult(
            mode=mode,
            status=RestoreVerificationStatus.ERROR,
            duration_seconds=0.0,
            message=exc.message,
            paths_requested=len(paths),
            checked_at=checked_at,
            restic_version=probe_restic_version(),
        )

    total_duration = 0.0
    snapshot_ids: list[str] = []
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sigint = signal.getsignal(signal.SIGINT)

    def _on_interrupt(signum: int, _frame: object) -> None:
        # Convert SIGTERM into an exception so finally cleanup always runs.
        raise KeyboardInterrupt(f"restore interrupted by signal {signum}")

    try:
        signal.signal(signal.SIGTERM, _on_interrupt)
        signal.signal(signal.SIGINT, _on_interrupt)
        by_snap: dict[str, list[str]] = {}
        for path, snap in per_path:
            by_snap.setdefault(snap.snapshot_id, []).append(path)

        for snap_id, group_paths in by_snap.items():
            snapshot_ids.append(snap_id)
            include = (
                tuple(group_paths)
                if mode is RestoreVerificationMode.SELECTED
                else tuple(group_paths)  # still scope to relevant paths for safety/disk
            )
            # full mode restores the same relevant audited paths; naming stays distinct
            # from integrity deep/standard and leaves room to broaden later.
            try:
                raw = restore_snapshot_paths(
                    repository=repository,
                    snapshot_id=snap_id,
                    target=owned.path,
                    include_paths=include,
                    password=password,
                    password_file=password_file,
                    password_ref=password_ref,
                    timeout=timeout,
                    verify=True,
                )
            except ResticError as exc:
                return RestoreVerificationResult(
                    mode=mode,
                    status=RestoreVerificationStatus.ERROR,
                    duration_seconds=total_duration,
                    message=exc.message,
                    snapshot_id=snap_id,
                    paths_requested=len(paths),
                    destination=str(owned.path),
                    checked_at=checked_at,
                    restic_version=probe_restic_version(),
                )
            total_duration += raw.duration_seconds
            if raw.returncode != 0:
                operational = _classify_restore_operational_failure(
                    returncode=raw.returncode,
                    output=raw.sanitized_output,
                )
                if operational is not None:
                    return RestoreVerificationResult(
                        mode=mode,
                        status=RestoreVerificationStatus.ERROR,
                        duration_seconds=total_duration,
                        message=operational,
                        snapshot_id=snap_id,
                        paths_requested=len(paths),
                        destination=str(owned.path),
                        checked_at=checked_at,
                        restic_version=probe_restic_version(),
                    )
                return RestoreVerificationResult(
                    mode=mode,
                    status=RestoreVerificationStatus.FAILED,
                    duration_seconds=total_duration,
                    message=(
                        "Restore verification failed: "
                        "Restic restore reported an error."
                    ),
                    snapshot_id=snap_id,
                    paths_requested=len(paths),
                    destination=str(owned.path),
                    checked_at=checked_at,
                    restic_version=probe_restic_version(),
                )

        validation = validate_restored_paths(
            restore_root=owned.path,
            source_paths=paths,
            expected_relative_paths=expected_paths,
        )
        if validation.status is not RestoreVerificationStatus.PASSED:
            return RestoreVerificationResult(
                mode=mode,
                status=RestoreVerificationStatus.FAILED,
                duration_seconds=total_duration,
                message=validation.message,
                snapshot_id=snapshot_ids[0] if snapshot_ids else None,
                paths_requested=len(paths),
                files_restored=validation.files_checked,
                bytes_restored=validation.bytes_checked,
                destination=str(owned.path),
                validation=validation,
                checked_at=checked_at,
                restic_version=probe_restic_version(),
            )

        label = (
            "selected-path restore verification passed"
            if mode is RestoreVerificationMode.SELECTED
            else "full-path restore verification passed"
        )
        return RestoreVerificationResult(
            mode=mode,
            status=RestoreVerificationStatus.PASSED,
            duration_seconds=total_duration,
            message=label,
            snapshot_id=snapshot_ids[0] if snapshot_ids else None,
            paths_requested=len(paths),
            paths_restored=len(paths),
            files_restored=validation.files_checked,
            bytes_restored=validation.bytes_checked,
            destination=str(owned.path),
            validation=validation,
            checked_at=checked_at,
            restic_version=probe_restic_version(),
        )
    except KeyboardInterrupt:
        return RestoreVerificationResult(
            mode=mode,
            status=RestoreVerificationStatus.ERROR,
            duration_seconds=total_duration,
            message=(
                "Restore verification interrupted; owned temp destination cleaned up."
            ),
            snapshot_id=snapshot_ids[0] if snapshot_ids else None,
            paths_requested=len(paths),
            destination=str(owned.path),
            checked_at=checked_at,
            restic_version=probe_restic_version(),
        )
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
        try:
            cleanup_owned_restore_root(owned)
        except RestoreDestinationError:
            pass
