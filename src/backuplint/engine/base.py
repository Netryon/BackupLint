"""Backup engine adapter contract (v1 interface for future Borg/Kopia)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from backuplint.config import IntegrityMode
from backuplint.engine.types import EngineCapabilities, OperationalKind, RepositoryStatus
from backuplint.restic import IntegrityCheckResult, ResticSnapshot


class BackupEngineError(Exception):
    """Normalized engine operational failure (distinct from coverage/integrity FAIL)."""

    def __init__(
        self,
        message: str,
        *,
        kind: OperationalKind,
        engine_id: str = "restic",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.engine_id = engine_id


@runtime_checkable
class BackupEngine(Protocol):
    """Small engine-neutral surface used by BackupLint audit/fleet paths."""

    @property
    def engine_id(self) -> str: ...

    def capabilities(self) -> EngineCapabilities: ...

    def repository_status(self) -> RepositoryStatus: ...

    def list_snapshots(self) -> list[ResticSnapshot]: ...

    def integrity_check(self, mode: IntegrityMode) -> IntegrityCheckResult: ...

    def classify_error(self, exc: BaseException) -> BackupEngineError: ...
