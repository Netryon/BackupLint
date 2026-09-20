"""SIEM export runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from backuplint.secrets import SecretRef, parse_secret_ref
from backuplint.secrets.ref import asdict_safe
from backuplint.siem.event import SiemEventFamily, SiemSeverity, severity_for_family
from backuplint.siem.queue import QueueLimits

SIEM_CONFIG_SCHEMA_VERSION = 1

_CRITICAL_HIGH_SEVERITIES = frozenset({SiemSeverity.CRITICAL, SiemSeverity.HIGH})


class SiemConfigError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SiemTransport(StrEnum):
    HTTPS_JSON = "https_json"
    SYSLOG_TLS = "syslog_tls"


class SiemAuthType(StrEnum):
    NONE = "none"
    BEARER = "bearer"


@dataclass(frozen=True)
class SiemConfig:
    enabled: bool = False
    transport: SiemTransport = SiemTransport.HTTPS_JSON
    endpoint: str = ""
    tls_verify: bool = True
    tls_ca_file: Path | None = None
    auth_type: SiemAuthType = SiemAuthType.NONE
    auth_token: SecretRef | None = None
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 10.0
    initial_backoff_seconds: float = 2.0
    max_backoff_seconds: float = 300.0
    jitter_ratio: float = 0.2
    max_attempts: int | None = None
    queue: QueueLimits = QueueLimits()
    min_severity: SiemSeverity = SiemSeverity.LOW
    allowed_families: frozenset[SiemEventFamily] | None = None
    batching_enabled: bool = False
    batch_max_size: int = 1
    schema_version: int = SIEM_CONFIG_SCHEMA_VERSION

    def to_safe_dict(self) -> dict[str, object]:
        return asdict_safe(self)

    def should_export(self, event_family: SiemEventFamily, severity: SiemSeverity) -> bool:
        if severity in _CRITICAL_HIGH_SEVERITIES:
            return True
        if not self._severity_passes_filter(severity):
            return False
        if self.allowed_families is None:
            return True
        return event_family in self.allowed_families

    def _severity_passes_filter(self, severity: SiemSeverity) -> bool:
        order = [
            SiemSeverity.INFO,
            SiemSeverity.LOW,
            SiemSeverity.MEDIUM,
            SiemSeverity.HIGH,
            SiemSeverity.CRITICAL,
        ]
        return order.index(severity) >= order.index(self.min_severity)


def parse_siem_config(
    raw: object,
    *,
    config_dir: Path | None = None,
    field_name: str = "siem",
) -> SiemConfig:
    if raw is None:
        return SiemConfig()
    if not isinstance(raw, dict):
        raise SiemConfigError(f"{field_name} must be a mapping.")

    allowed_top = {
        "siem_config_schema_version",
        "enabled",
        "transport",
        "endpoint",
        "tls",
        "auth",
        "timeouts",
        "retry",
        "queue",
        "filters",
        "batching",
    }
    unknown = sorted(set(raw) - allowed_top)
    if unknown:
        raise SiemConfigError(
            f"{field_name} has unknown key(s): " + ", ".join(repr(k) for k in unknown)
        )

    version = raw.get("siem_config_schema_version", SIEM_CONFIG_SCHEMA_VERSION)
    if not isinstance(version, int) or isinstance(version, bool):
        raise SiemConfigError(f"{field_name}.siem_config_schema_version must be an integer")
    if version != SIEM_CONFIG_SCHEMA_VERSION:
        raise SiemConfigError(
            f"unsupported siem_config_schema_version {version}; "
            f"expected {SIEM_CONFIG_SCHEMA_VERSION}"
        )

    enabled = _bool(raw.get("enabled"), default=False, field=f"{field_name}.enabled")
    transport = _enum(
        raw.get("transport", SiemTransport.HTTPS_JSON.value),
        SiemTransport,
        field=f"{field_name}.transport",
    )
    if transport is not SiemTransport.HTTPS_JSON:
        raise SiemConfigError(
            f"{field_name}.transport {transport.value} is not supported in v0.7"
        )

    endpoint = str(raw.get("endpoint") or "").strip()
    if enabled and not endpoint:
        raise SiemConfigError(f"{field_name}.endpoint is required when enabled=true")
    if endpoint and not endpoint.lower().startswith("https://"):
        raise SiemConfigError(f"{field_name}.endpoint must use https://")

    tls = raw.get("tls") or {}
    if tls is not None and not isinstance(tls, dict):
        raise SiemConfigError(f"{field_name}.tls must be a mapping")
    tls_verify = _bool(
        tls.get("verify", True) if isinstance(tls, dict) else True,
        default=True,
        field=f"{field_name}.tls.verify",
    )
    ca_raw = tls.get("ca_file") if isinstance(tls, dict) else None
    tls_ca_file = None
    if ca_raw is not None:
        if not isinstance(ca_raw, str) or not ca_raw.strip():
            raise SiemConfigError(f"{field_name}.tls.ca_file must be a non-empty string")
        tls_ca_file = Path(ca_raw.strip()).expanduser()

    auth = raw.get("auth") or {}
    if auth is not None and not isinstance(auth, dict):
        raise SiemConfigError(f"{field_name}.auth must be a mapping")
    auth_type = _enum(
        auth.get("type", SiemAuthType.NONE.value) if isinstance(auth, dict) else "none",
        SiemAuthType,
        field=f"{field_name}.auth.type",
    )
    auth_token: SecretRef | None = None
    if auth_type is SiemAuthType.BEARER:
        token_raw = auth.get("token") if isinstance(auth, dict) else None
        if token_raw is None:
            raise SiemConfigError(f"{field_name}.auth.token is required for bearer auth")
        auth_token = parse_secret_ref(
            token_raw,
            config_dir=config_dir,
            field_name=f"{field_name}.auth.token",
        )
    elif isinstance(auth, dict) and auth.get("token") is not None:
        raise SiemConfigError(f"{field_name}.auth.token requires auth.type=bearer")

    timeouts = raw.get("timeouts") or {}
    if timeouts is not None and not isinstance(timeouts, dict):
        raise SiemConfigError(f"{field_name}.timeouts must be a mapping")
    connect_timeout = _positive_float(
        timeouts.get("connect_seconds", 5) if isinstance(timeouts, dict) else 5,
        field=f"{field_name}.timeouts.connect_seconds",
    )
    read_timeout = _positive_float(
        timeouts.get("read_seconds", 10) if isinstance(timeouts, dict) else 10,
        field=f"{field_name}.timeouts.read_seconds",
    )

    retry = raw.get("retry") or {}
    if retry is not None and not isinstance(retry, dict):
        raise SiemConfigError(f"{field_name}.retry must be a mapping")
    initial_backoff = _positive_float(
        retry.get("initial_backoff_seconds", 2) if isinstance(retry, dict) else 2,
        field=f"{field_name}.retry.initial_backoff_seconds",
    )
    max_backoff = _positive_float(
        retry.get("max_backoff_seconds", 300) if isinstance(retry, dict) else 300,
        field=f"{field_name}.retry.max_backoff_seconds",
    )
    jitter_ratio = _ratio(
        retry.get("jitter_ratio", 0.2) if isinstance(retry, dict) else 0.2,
        field=f"{field_name}.retry.jitter_ratio",
    )
    max_attempts_raw = retry.get("max_attempts") if isinstance(retry, dict) else None
    max_attempts = None
    if max_attempts_raw is not None:
        if not isinstance(max_attempts_raw, int) or isinstance(max_attempts_raw, bool):
            raise SiemConfigError(f"{field_name}.retry.max_attempts must be an integer")
        max_attempts = max(1, int(max_attempts_raw))

    queue = _parse_queue_limits(raw.get("queue"), field_name=field_name)
    filters = raw.get("filters") or {}
    if filters is not None and not isinstance(filters, dict):
        raise SiemConfigError(f"{field_name}.filters must be a mapping")
    min_severity = _enum(
        filters.get("min_severity", SiemSeverity.LOW.value)
        if isinstance(filters, dict)
        else SiemSeverity.LOW.value,
        SiemSeverity,
        field=f"{field_name}.filters.min_severity",
    )
    allowed_families = _parse_families_filter(
        filters.get("families") if isinstance(filters, dict) else None,
        field_name=field_name,
    )

    batching = raw.get("batching") or {}
    if batching is not None and not isinstance(batching, dict):
        raise SiemConfigError(f"{field_name}.batching must be a mapping")
    batching_enabled = _bool(
        batching.get("enabled", False) if isinstance(batching, dict) else False,
        default=False,
        field=f"{field_name}.batching.enabled",
    )
    batch_max_size = 1
    if isinstance(batching, dict) and "max_batch_size" in batching:
        raw_size = batching.get("max_batch_size")
        if not isinstance(raw_size, int) or isinstance(raw_size, bool) or raw_size < 1:
            raise SiemConfigError(f"{field_name}.batching.max_batch_size must be >= 1")
        batch_max_size = int(raw_size)

    if enabled and auth_type is SiemAuthType.NONE:
        raise SiemConfigError(
            f"{field_name}.auth.type must be bearer when enabled=true"
        )

    return SiemConfig(
        enabled=enabled,
        transport=transport,
        endpoint=endpoint,
        tls_verify=tls_verify,
        tls_ca_file=tls_ca_file,
        auth_type=auth_type,
        auth_token=auth_token,
        connect_timeout_seconds=connect_timeout,
        read_timeout_seconds=read_timeout,
        initial_backoff_seconds=initial_backoff,
        max_backoff_seconds=max_backoff,
        jitter_ratio=jitter_ratio,
        max_attempts=max_attempts,
        queue=queue,
        min_severity=min_severity,
        allowed_families=allowed_families,
        batching_enabled=batching_enabled,
        batch_max_size=batch_max_size,
        schema_version=version,
    )


def _parse_queue_limits(raw: object, *, field_name: str) -> QueueLimits:
    if raw is None:
        return QueueLimits()
    if not isinstance(raw, dict):
        raise SiemConfigError(f"{field_name}.queue must be a mapping")
    max_items = _positive_int(raw.get("max_items", 20_000), field=f"{field_name}.queue.max_items")
    soft_cap_ratio = _ratio(
        raw.get("soft_cap_ratio", 0.8),
        field=f"{field_name}.queue.soft_cap_ratio",
    )
    reserved_critical = _positive_int(
        raw.get("reserved_critical", 5_000),
        field=f"{field_name}.queue.reserved_critical",
    )
    delivered_retention_hours = _positive_int(
        raw.get("delivered_retention_hours", 24),
        field=f"{field_name}.queue.delivered_retention_hours",
    )
    dead_letter_retention_days = _positive_int(
        raw.get("dead_letter_retention_days", 30),
        field=f"{field_name}.queue.dead_letter_retention_days",
    )
    if reserved_critical >= max_items:
        raise SiemConfigError(
            f"{field_name}.queue.reserved_critical must be less than max_items"
        )
    return QueueLimits(
        max_items=max_items,
        soft_cap_ratio=soft_cap_ratio,
        reserved_critical=reserved_critical,
        delivered_retention_hours=delivered_retention_hours,
        dead_letter_retention_days=dead_letter_retention_days,
    )


def _parse_families_filter(
    raw: object,
    *,
    field_name: str,
) -> frozenset[SiemEventFamily] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise SiemConfigError(f"{field_name}.filters.families must be a list or null")
    families: set[SiemEventFamily] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise SiemConfigError(
                f"{field_name}.filters.families[{index}] must be a non-empty string"
            )
        try:
            family = SiemEventFamily(item.strip())
        except ValueError as exc:
            raise SiemConfigError(
                f"{field_name}.filters.families[{index}] is unknown: {item!r}"
            ) from exc
        families.add(family)
    _validate_families_filter(families, field_name=field_name)
    return frozenset(families)


def _validate_families_filter(
    families: set[SiemEventFamily],
    *,
    field_name: str,
) -> None:
    for family in SiemEventFamily:
        if severity_for_family(family) in _CRITICAL_HIGH_SEVERITIES and family not in families:
            raise SiemConfigError(
                f"{field_name}.filters.families cannot exclude critical/high family "
                f"{family.value}"
            )


def _bool(raw: object, *, default: bool, field: str) -> bool:
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise SiemConfigError(f"{field} must be a boolean")
    return raw


def _positive_int(raw: object, *, field: str) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        raise SiemConfigError(f"{field} must be a positive integer")
    return int(raw)


def _positive_float(raw: object, *, field: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or float(raw) <= 0:
        raise SiemConfigError(f"{field} must be a positive number")
    return float(raw)


def _ratio(raw: object, *, field: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise SiemConfigError(f"{field} must be a number")
    value = float(raw)
    if value <= 0 or value > 1:
        raise SiemConfigError(f"{field} must be in (0, 1]")
    return value


def _enum(raw: object, enum_type: type, *, field: str):
    if not isinstance(raw, str) or not raw.strip():
        raise SiemConfigError(f"{field} must be a non-empty string")
    try:
        return enum_type(raw.strip())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise SiemConfigError(f"{field} must be one of: {allowed}") from exc
