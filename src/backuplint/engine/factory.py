"""Resolve the configured backup engine (v1: Restic only)."""

from __future__ import annotations

from backuplint.config import BackupLintConfig, ResticConfig
from backuplint.engine.base import BackupEngine, BackupEngineError
from backuplint.engine.restic_engine import ResticBackupEngine, _kind_from_restic_message
from backuplint.engine.types import OperationalKind
from backuplint.errors import ConfigError
from backuplint.restic import ResticError


def get_backup_engine(restic: ResticConfig) -> BackupEngine:
    """Return the Restic adapter for a restic config section."""
    return ResticBackupEngine(restic)


def engine_from_config(config: BackupLintConfig) -> BackupEngine:
    """Resolve engine from full BackupLint config.

    Existing configs without an explicit engine discriminator default to Restic.
    Borg/Kopia are not selectable in v1.
    """
    if config.restic is None:
        raise ConfigError("No backup engine configured (restic section required).")
    return get_backup_engine(config.restic)


def classify_engine_exception(
    exc: BaseException, *, engine_id: str = "restic"
) -> BackupEngineError:
    """Normalize Restic/adapter exceptions into BackupEngineError."""
    if isinstance(exc, BackupEngineError):
        return exc
    if isinstance(exc, ResticError):
        return BackupEngineError(
            exc.message,
            kind=_kind_from_restic_message(exc.message),
            engine_id=engine_id,
        )
    return BackupEngineError(
        str(exc) or "backup engine runtime failure",
        kind=OperationalKind.RUNTIME,
        engine_id=engine_id,
    )


__all__ = [
    "BackupEngine",
    "BackupEngineError",
    "OperationalKind",
    "ResticBackupEngine",
    "classify_engine_exception",
    "engine_from_config",
    "get_backup_engine",
]
