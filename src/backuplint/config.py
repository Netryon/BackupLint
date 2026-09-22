"""Load BackupLint configuration files."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from pathlib import Path

import yaml

from backuplint.errors import ConfigError
from backuplint.fleet_config import FleetConfig, parse_fleet
from backuplint.schedule_config import ScheduleConfig, parse_schedule
from backuplint.siem.config import SiemConfig, SiemConfigError, parse_siem_config
from backuplint.timeutil import parse_duration

__all__ = [
    "BackupLintConfig",
    "CONFIG_SCHEMA_VERSION",
    "ConfigError",
    "FleetConfig",
    "IntegrityConfig",
    "IntegrityMode",
    "RestoreVerificationConfig",
    "RestoreVerificationMode",
    "ResticConfig",
    "ScheduleConfig",
    "SiemConfig",
    "default_config_path",
    "load_config",
    "parse_config_data",
    "parse_integrity_mode",
    "parse_restore_verification_mode",
]


class IntegrityMode(StrEnum):
    OFF = "off"
    STANDARD = "standard"
    DEEP = "deep"


class RestoreVerificationMode(StrEnum):
    OFF = "off"
    SELECTED = "selected"
    FULL = "full"


@dataclass(frozen=True)
class IntegrityConfig:
    mode: IntegrityMode = IntegrityMode.OFF
    max_age: timedelta | None = None
    state_file: Path | None = None


@dataclass(frozen=True)
class RestoreVerificationConfig:
    mode: RestoreVerificationMode = RestoreVerificationMode.OFF
    timeout: timedelta | None = None
    expected_paths: tuple[str, ...] = ()
    state_file: Path | None = None
    max_age: timedelta | None = None


@dataclass(frozen=True)
class ResticConfig:
    repository: str
    password_file: Path | None = None
    password: object | None = None  # SecretRef | None; typed loosely to avoid cycles
    integrity: IntegrityConfig = field(default_factory=IntegrityConfig)
    restore_verification: RestoreVerificationConfig = field(
        default_factory=RestoreVerificationConfig
    )


@dataclass(frozen=True)
class BackupLintConfig:
    backup_paths: tuple[str, ...]
    restic: ResticConfig | None = None
    max_backup_age: timedelta | None = None
    schedule: ScheduleConfig | None = None
    fleet: FleetConfig | None = None
    siem: SiemConfig | None = None
    source_file: Path | None = None
    schema_version: int = 1
    compose_files: tuple[Path, ...] = ()


def _parse_backup_paths(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("'backup_paths' must be a list of path strings.")

    paths: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(
                f"'backup_paths[{index}]' must be a non-empty string path."
            )
        value = item.strip()
        if value in seen:
            continue
        seen.add(value)
        paths.append(value)
    return tuple(paths)


def _parse_compose_files(
    raw: object, *, config_dir: Path | None
) -> tuple[Path, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("'compose_files' must be a list of path strings.")
    files: list[Path] = []
    seen: set[Path] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(
                f"'compose_files[{index}]' must be a non-empty string path."
            )
        path = _resolve_config_path(item, config_dir=config_dir)
        if path in seen:
            continue
        seen.add(path)
        files.append(path)
    return tuple(files)


def _resolve_config_path(raw: str, *, config_dir: Path | None) -> Path:
    candidate = Path(raw.strip()).expanduser()
    if not candidate.is_absolute() and config_dir is not None:
        return (config_dir / candidate).resolve()
    return candidate.expanduser()


def _parse_integrity(raw: object, *, config_dir: Path | None) -> IntegrityConfig:
    if raw is None:
        return IntegrityConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'restic.integrity' must be a mapping.")

    allowed = {"mode", "max_age", "state_file"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(
            "Unknown restic.integrity key(s): "
            + ", ".join(repr(key) for key in unknown)
        )

    mode = IntegrityMode.OFF
    mode_raw = raw.get("mode")
    if mode_raw is not None:
        # YAML may parse unquoted off/on/yes/no as booleans.
        if isinstance(mode_raw, bool):
            if mode_raw is False:
                mode = IntegrityMode.OFF
            else:
                raise ConfigError(
                    "'restic.integrity.mode' must be one of: off, standard, deep "
                    "(quote 'off' in YAML if needed)."
                )
        elif not isinstance(mode_raw, str) or not mode_raw.strip():
            raise ConfigError(
                "'restic.integrity.mode' must be one of: off, standard, deep."
            )
        else:
            normalized = mode_raw.strip().lower()
            try:
                mode = IntegrityMode(normalized)
            except ValueError as exc:
                raise ConfigError(
                    "'restic.integrity.mode' must be one of: off, standard, deep."
                ) from exc

    max_age: timedelta | None = None
    max_age_raw = raw.get("max_age")
    if max_age_raw is not None:
        if not isinstance(max_age_raw, str) or not max_age_raw.strip():
            raise ConfigError(
                "'restic.integrity.max_age' must be a non-empty duration string."
            )
        try:
            max_age = parse_duration(max_age_raw)
        except ValueError as exc:
            raise ConfigError(f"Invalid restic.integrity.max_age: {exc}") from exc

    state_file: Path | None = None
    state_file_raw = raw.get("state_file")
    if state_file_raw is not None:
        if not isinstance(state_file_raw, str) or not state_file_raw.strip():
            raise ConfigError(
                "'restic.integrity.state_file' must be a non-empty string path."
            )
        state_file = _resolve_config_path(state_file_raw, config_dir=config_dir)

    return IntegrityConfig(mode=mode, max_age=max_age, state_file=state_file)


def _parse_expected_paths(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(
            "'restic.restore_verification.expected_paths' must be a list of strings."
        )
    paths: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(
                f"'restic.restore_verification.expected_paths[{index}]' "
                "must be a non-empty string."
            )
        value = item.strip()
        if value in seen:
            continue
        seen.add(value)
        paths.append(value)
    return tuple(paths)


def _parse_restore_verification(
    raw: object, *, config_dir: Path | None
) -> RestoreVerificationConfig:
    if raw is None:
        return RestoreVerificationConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'restic.restore_verification' must be a mapping.")

    allowed = {"mode", "timeout", "expected_paths", "state_file", "max_age"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(
            "Unknown restic.restore_verification key(s): "
            + ", ".join(repr(key) for key in unknown)
        )

    mode = RestoreVerificationMode.OFF
    mode_raw = raw.get("mode")
    if mode_raw is not None:
        if isinstance(mode_raw, bool):
            if mode_raw is False:
                mode = RestoreVerificationMode.OFF
            else:
                raise ConfigError(
                    "'restic.restore_verification.mode' must be one of: "
                    "off, selected, full (quote 'off' in YAML if needed)."
                )
        elif not isinstance(mode_raw, str) or not mode_raw.strip():
            raise ConfigError(
                "'restic.restore_verification.mode' must be one of: "
                "off, selected, full."
            )
        else:
            normalized = mode_raw.strip().lower()
            try:
                mode = RestoreVerificationMode(normalized)
            except ValueError as exc:
                raise ConfigError(
                    "'restic.restore_verification.mode' must be one of: "
                    "off, selected, full."
                ) from exc

    timeout: timedelta | None = None
    timeout_raw = raw.get("timeout")
    if timeout_raw is not None:
        if not isinstance(timeout_raw, str) or not timeout_raw.strip():
            raise ConfigError(
                "'restic.restore_verification.timeout' must be a non-empty "
                "duration string."
            )
        try:
            timeout = parse_duration(timeout_raw)
        except ValueError as exc:
            raise ConfigError(
                f"Invalid restic.restore_verification.timeout: {exc}"
            ) from exc
        if timeout.total_seconds() <= 0:
            raise ConfigError(
                "'restic.restore_verification.timeout' must be greater than zero."
            )

    max_age: timedelta | None = None
    max_age_raw = raw.get("max_age")
    if max_age_raw is not None:
        if not isinstance(max_age_raw, str) or not max_age_raw.strip():
            raise ConfigError(
                "'restic.restore_verification.max_age' must be a non-empty "
                "duration string."
            )
        try:
            max_age = parse_duration(max_age_raw)
        except ValueError as exc:
            raise ConfigError(
                f"Invalid restic.restore_verification.max_age: {exc}"
            ) from exc

    state_file: Path | None = None
    state_file_raw = raw.get("state_file")
    if state_file_raw is not None:
        if not isinstance(state_file_raw, str) or not state_file_raw.strip():
            raise ConfigError(
                "'restic.restore_verification.state_file' must be a non-empty "
                "string path."
            )
        state_file = _resolve_config_path(state_file_raw, config_dir=config_dir)

    expected_paths = _parse_expected_paths(raw.get("expected_paths"))
    return RestoreVerificationConfig(
        mode=mode,
        timeout=timeout,
        expected_paths=expected_paths,
        state_file=state_file,
        max_age=max_age,
    )


def _parse_restic(raw: object, *, config_dir: Path | None) -> ResticConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'restic' must be a mapping.")

    allowed = {
        "repository",
        "password_file",
        "password",
        "integrity",
        "restore_verification",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(
            "Unknown restic key(s): " + ", ".join(repr(key) for key in unknown)
        )

    repository = raw.get("repository")
    if not isinstance(repository, str) or not repository.strip():
        raise ConfigError("'restic.repository' must be a non-empty string.")

    password_file_raw = raw.get("password_file")
    password_raw = raw.get("password")
    if password_file_raw is not None and password_raw is not None:
        raise ConfigError(
            "Configure only one of restic.password_file or restic.password."
        )

    password_file: Path | None = None
    password_ref = None
    if password_file_raw is not None:
        if not isinstance(password_file_raw, str) or not password_file_raw.strip():
            raise ConfigError("'restic.password_file' must be a non-empty string path.")
        password_file = _resolve_config_path(password_file_raw, config_dir=config_dir)
    if password_raw is not None:
        from backuplint.secrets import InvalidSecretRefError, parse_secret_ref

        try:
            password_ref = parse_secret_ref(
                password_raw,
                config_dir=config_dir,
                field_name="restic.password",
            )
        except InvalidSecretRefError as exc:
            raise ConfigError(exc.message) from exc

    integrity = _parse_integrity(raw.get("integrity"), config_dir=config_dir)
    restore_verification = _parse_restore_verification(
        raw.get("restore_verification"),
        config_dir=config_dir,
    )
    return ResticConfig(
        repository=repository.strip(),
        password_file=password_file,
        password=password_ref,
        integrity=integrity,
        restore_verification=restore_verification,
    )


def _parse_max_backup_age(raw: object) -> timedelta | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError("'max_backup_age' must be a non-empty duration string.")
    try:
        return parse_duration(raw)
    except ValueError as exc:
        raise ConfigError(f"Invalid max_backup_age: {exc}") from exc


CONFIG_SCHEMA_VERSION = 1


def _parse_schema_version(raw: object) -> int:
    """Resolve config schema version.

    Policy: missing ``schema_version`` is treated as legacy schema 1 (current
    v0.5 configuration). Explicit unsupported future versions are rejected.
    """
    if raw is None:
        return CONFIG_SCHEMA_VERSION
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError("'schema_version' must be an integer.")
    if raw == CONFIG_SCHEMA_VERSION:
        return raw
    if raw < 1:
        raise ConfigError(f"Unsupported configuration schema_version: {raw}")
    raise ConfigError(
        f"Unsupported configuration schema_version: {raw} "
        f"(this BackupLint build supports {CONFIG_SCHEMA_VERSION})."
    )


def parse_config_data(
    data: object,
    *,
    source_file: Path | None = None,
) -> BackupLintConfig:
    """Parse an in-memory configuration mapping."""
    if data is None:
        raise ConfigError("Configuration file is empty.")
    if not isinstance(data, dict):
        raise ConfigError("Configuration root must be a mapping.")

    if "backup_paths" not in data:
        raise ConfigError("Configuration must define 'backup_paths'.")

    unknown = sorted(
        set(data)
        - {
            "backup_paths",
            "restic",
            "max_backup_age",
            "schedule",
            "fleet",
            "siem",
            "schema_version",
            "compose_files",
        }
    )
    if unknown:
        raise ConfigError(
            "Unknown configuration key(s): " + ", ".join(repr(key) for key in unknown)
        )

    schema_version = _parse_schema_version(data.get("schema_version"))
    config_dir = source_file.parent if source_file is not None else None
    restic = _parse_restic(data.get("restic"), config_dir=config_dir)
    max_backup_age = _parse_max_backup_age(data.get("max_backup_age"))
    if max_backup_age is not None and restic is None:
        raise ConfigError("'max_backup_age' requires a 'restic' configuration section.")
    schedule = parse_schedule(data.get("schedule"), config_dir=config_dir)
    fleet = parse_fleet(data.get("fleet"), config_dir=config_dir)
    siem_raw = data.get("siem")
    siem: SiemConfig | None
    if siem_raw is None:
        siem = None
    else:
        try:
            siem = parse_siem_config(siem_raw, config_dir=config_dir)
        except SiemConfigError as exc:
            raise ConfigError(exc.message) from exc

    return BackupLintConfig(
        backup_paths=_parse_backup_paths(data.get("backup_paths")),
        restic=restic,
        max_backup_age=max_backup_age,
        schedule=schedule,
        fleet=fleet,
        siem=siem,
        source_file=source_file,
        schema_version=schema_version,
        compose_files=_parse_compose_files(
            data.get("compose_files"), config_dir=config_dir
        ),
    )


def load_config(config_file: Path) -> BackupLintConfig:
    """Load BackupLint YAML configuration from disk."""
    path = config_file.expanduser()
    if not path.exists():
        raise ConfigError(f"Configuration file not found: {path}")
    if not path.is_file():
        raise ConfigError(f"Configuration path is not a file: {path}")

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Unable to read configuration file: {exc}") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Malformed configuration YAML:\n{exc}") from exc

    return parse_config_data(data, source_file=path.resolve())


def default_config_path(compose_file: Path) -> Path:
    """Prefer backuplint.yml beside the Compose file, then in the current directory."""
    beside = compose_file.expanduser().resolve().parent / "backuplint.yml"
    if beside.is_file():
        return beside
    return Path.cwd() / "backuplint.yml"


def parse_integrity_mode(value: str) -> IntegrityMode:
    """Parse a CLI or config integrity mode string."""
    normalized = value.strip().lower()
    try:
        return IntegrityMode(normalized)
    except ValueError as exc:
        raise ConfigError(
            "Integrity mode must be one of: off, standard, deep."
        ) from exc


def parse_restore_verification_mode(value: str) -> RestoreVerificationMode:
    """Parse a CLI or config restore-verification mode string."""
    normalized = value.strip().lower()
    try:
        return RestoreVerificationMode(normalized)
    except ValueError as exc:
        raise ConfigError(
            "Restore verification mode must be one of: off, selected, full."
        ) from exc
