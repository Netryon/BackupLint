"""Restic implementation of the BackupEngine adapter (only v1 engine)."""

from __future__ import annotations

from backuplint.config import IntegrityMode, ResticConfig
from backuplint.engine.base import BackupEngineError
from backuplint.engine.types import EngineCapabilities, OperationalKind, RepositoryStatus
from backuplint.restic import (
    IntegrityCheckResult,
    ResticError,
    ResticSnapshot,
    check_repository_integrity,
    list_snapshots,
    probe_restic_version,
)
from backuplint.restic_errors import (
    ResticOperationalKind,
    classify_list_snapshots_failure,
)


def _map_restic_kind(kind: ResticOperationalKind | None) -> OperationalKind:
    if kind is ResticOperationalKind.AUTH:
        return OperationalKind.AUTHENTICATION
    if kind is ResticOperationalKind.LOCKED:
        return OperationalKind.LOCKED
    if kind is ResticOperationalKind.NOT_FOUND:
        return OperationalKind.REPOSITORY_UNAVAILABLE
    if kind is ResticOperationalKind.NETWORK:
        return OperationalKind.NETWORK
    if kind is ResticOperationalKind.PERMISSION:
        return OperationalKind.PERMISSION
    return OperationalKind.RUNTIME


def _kind_from_restic_message(message: str) -> OperationalKind:
    lower = (message or "").lower()
    if "authentication failed" in lower:
        return OperationalKind.AUTHENTICATION
    if (
        "repository is locked" in lower
        or "already locked" in lower
        or "empty snapshot json" in lower
        or "produced no output" in lower
    ):
        return OperationalKind.LOCKED
    if "permission denied" in lower:
        return OperationalKind.PERMISSION
    if "backend unavailable" in lower or "network failure" in lower:
        return OperationalKind.NETWORK
    if "inaccessible or missing" in lower or "not a repository" in lower:
        return OperationalKind.REPOSITORY_UNAVAILABLE
    if "does not exist" in lower or "unable to open" in lower:
        return OperationalKind.REPOSITORY_UNAVAILABLE
    return OperationalKind.RUNTIME


class ResticBackupEngine:
    """Adapter around existing Restic helpers — preserves Restic CLI semantics."""

    def __init__(self, config: ResticConfig) -> None:
        self._config = config

    @property
    def engine_id(self) -> str:
        return "restic"

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            engine_id=self.engine_id,
            list_snapshots=True,
            standard_integrity=True,
            deep_integrity=True,
            isolated_restore=True,
            version_reporting=True,
        )

    def repository_status(self) -> RepositoryStatus:
        try:
            self.list_snapshots()
        except BackupEngineError as exc:
            return RepositoryStatus(
                available=False,
                engine_id=self.engine_id,
                message=exc.message,
                kind=exc.kind,
            )
        version: str | None
        try:
            version = probe_restic_version()
        except Exception:  # noqa: BLE001 — best-effort metadata only
            version = None
        return RepositoryStatus(
            available=True,
            engine_id=self.engine_id,
            message="repository available",
            engine_version=version,
        )

    def list_snapshots(self) -> list[ResticSnapshot]:
        try:
            return list_snapshots(
                repository=self._config.repository,
                password_file=self._config.password_file,
                password_ref=self._config.password,
            )
        except ResticError as exc:
            raise self.classify_error(exc) from exc

    def integrity_check(self, mode: IntegrityMode) -> IntegrityCheckResult:
        try:
            return check_repository_integrity(
                mode=mode,
                repository=self._config.repository,
                password_file=self._config.password_file,
                password_ref=self._config.password,
            )
        except ResticError as exc:
            raise self.classify_error(exc) from exc

    def classify_error(self, exc: BaseException) -> BackupEngineError:
        if isinstance(exc, BackupEngineError):
            return exc
        if isinstance(exc, ResticError):
            message = exc.message
            kind = _kind_from_restic_message(message)
            if "Restic snapshot listing failed" in message or (
                "\n" in message and "Unable to open Restic" not in message
            ):
                detail = message.split("\n", 1)[-1] if "\n" in message else message
                mapped = _map_restic_kind(classify_list_snapshots_failure(detail))
                if mapped is not OperationalKind.RUNTIME:
                    kind = mapped
            return BackupEngineError(message, kind=kind, engine_id=self.engine_id)
        return BackupEngineError(
            str(exc) or "backup engine runtime failure",
            kind=OperationalKind.RUNTIME,
            engine_id=self.engine_id,
        )
