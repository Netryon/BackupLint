"""Backup engine adapters (v1: Restic only; Borg/Kopia reserved for later)."""

from backuplint.engine.base import BackupEngine, BackupEngineError
from backuplint.engine.factory import (
    classify_engine_exception,
    engine_from_config,
    get_backup_engine,
)
from backuplint.engine.restic_engine import ResticBackupEngine
from backuplint.engine.types import EngineCapabilities, OperationalKind, RepositoryStatus

__all__ = [
    "BackupEngine",
    "BackupEngineError",
    "EngineCapabilities",
    "OperationalKind",
    "RepositoryStatus",
    "ResticBackupEngine",
    "classify_engine_exception",
    "engine_from_config",
    "get_backup_engine",
]
