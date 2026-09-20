"""Normalized SIEM export event model and conversion from internal domain events."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from backuplint.events import (
    CanonicalEvent,
    EventType,
    assert_payload_safe,
    status_from_audit_result,
)

SIEM_EVENT_SCHEMA_VERSION = 1

_EVENT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_SUMMARY_MAX_LEN = 500
_DETAILS_MAX_KEYS = 32
_DETAILS_MAX_JSON_BYTES = 4096
_SIEM_ID_NAMESPACE = uuid.UUID("7c9e6679-7425-40de-944b-e07fc1f90ae7")

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


class SiemEventError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SiemSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class SiemEventFamily(StrEnum):
    BACKUP_COVERAGE_FAILED = "backup_coverage_failed"
    INTEGRITY_CHECK_FAILED = "integrity_check_failed"
    RESTORE_VERIFICATION_FAILED = "restore_verification_failed"
    RESTORE_VERIFICATION_STALE = "restore_verification_stale"
    AGENT_OFFLINE = "agent_offline"
    AGENT_ONLINE = "agent_online"
    AGENT_AUTHENTICATION_FAILED = "agent_authentication_failed"
    DATA_GAP = "data_gap"
    QUEUE_OVERFLOW = "queue_overflow"
    REPOSITORY_UNAVAILABLE = "repository_unavailable"
    CONTROLLER_OPERATIONAL_ERROR = "controller_operational_error"
    SIEM_DELIVERY_ERROR = "siem_delivery_error"


class SiemCategory(StrEnum):
    BACKUP_ASSURANCE = "backup_assurance"
    FLEET_TRANSPORT = "fleet_transport"
    SIEM_DELIVERY = "siem_delivery"
    CONTROLLER_OPERATIONAL = "controller_operational"
    SECURITY_AUDIT = "security_audit"


NON_EXPORTABLE_FAMILIES = frozenset({SiemEventFamily.SIEM_DELIVERY_ERROR})

_FAMILY_CATEGORY: dict[SiemEventFamily, SiemCategory] = {
    SiemEventFamily.BACKUP_COVERAGE_FAILED: SiemCategory.BACKUP_ASSURANCE,
    SiemEventFamily.INTEGRITY_CHECK_FAILED: SiemCategory.BACKUP_ASSURANCE,
    SiemEventFamily.RESTORE_VERIFICATION_FAILED: SiemCategory.BACKUP_ASSURANCE,
    SiemEventFamily.RESTORE_VERIFICATION_STALE: SiemCategory.BACKUP_ASSURANCE,
    SiemEventFamily.REPOSITORY_UNAVAILABLE: SiemCategory.BACKUP_ASSURANCE,
    SiemEventFamily.AGENT_OFFLINE: SiemCategory.FLEET_TRANSPORT,
    SiemEventFamily.AGENT_ONLINE: SiemCategory.FLEET_TRANSPORT,
    SiemEventFamily.DATA_GAP: SiemCategory.FLEET_TRANSPORT,
    SiemEventFamily.QUEUE_OVERFLOW: SiemCategory.FLEET_TRANSPORT,
    SiemEventFamily.AGENT_AUTHENTICATION_FAILED: SiemCategory.SECURITY_AUDIT,
    SiemEventFamily.CONTROLLER_OPERATIONAL_ERROR: SiemCategory.CONTROLLER_OPERATIONAL,
    SiemEventFamily.SIEM_DELIVERY_ERROR: SiemCategory.SIEM_DELIVERY,
}

_FAMILY_SEVERITY: dict[SiemEventFamily, SiemSeverity] = {
    SiemEventFamily.BACKUP_COVERAGE_FAILED: SiemSeverity.HIGH,
    SiemEventFamily.INTEGRITY_CHECK_FAILED: SiemSeverity.HIGH,
    SiemEventFamily.RESTORE_VERIFICATION_FAILED: SiemSeverity.HIGH,
    SiemEventFamily.RESTORE_VERIFICATION_STALE: SiemSeverity.MEDIUM,
    SiemEventFamily.AGENT_OFFLINE: SiemSeverity.MEDIUM,
    SiemEventFamily.AGENT_ONLINE: SiemSeverity.LOW,
    SiemEventFamily.AGENT_AUTHENTICATION_FAILED: SiemSeverity.CRITICAL,
    SiemEventFamily.DATA_GAP: SiemSeverity.HIGH,
    SiemEventFamily.QUEUE_OVERFLOW: SiemSeverity.CRITICAL,
    SiemEventFamily.REPOSITORY_UNAVAILABLE: SiemSeverity.MEDIUM,
    SiemEventFamily.CONTROLLER_OPERATIONAL_ERROR: SiemSeverity.LOW,
    SiemEventFamily.SIEM_DELIVERY_ERROR: SiemSeverity.INFO,
}

_AUDIT_FAILURE_STATUSES = frozenset({"FAIL", "ERROR"})
_EVENT_TYPE_FAMILY: dict[EventType, SiemEventFamily] = {
    EventType.COVERAGE_RESULT: SiemEventFamily.BACKUP_COVERAGE_FAILED,
    EventType.INTEGRITY_RESULT: SiemEventFamily.INTEGRITY_CHECK_FAILED,
    EventType.RESTORE_RESULT: SiemEventFamily.RESTORE_VERIFICATION_FAILED,
    EventType.AUDIT_COMPLETED: SiemEventFamily.BACKUP_COVERAGE_FAILED,
}


def _sanitize_summary(text: str) -> str:
    cleaned = _CONTROL_CHAR_RE.sub("", text.strip())
    if len(cleaned) > _SUMMARY_MAX_LEN:
        return cleaned[: _SUMMARY_MAX_LEN - 3] + "..."
    return cleaned


def _sanitize_details(details: dict[str, Any] | None) -> dict[str, Any] | None:
    if not details:
        return None
    assert_payload_safe(details)
    bounded: dict[str, Any] = {}
    for index, (key, value) in enumerate(details.items()):
        if index >= _DETAILS_MAX_KEYS:
            break
        if isinstance(value, str):
            bounded[str(key)] = _sanitize_summary(value)
        elif isinstance(value, (int, float, bool)) or value is None:
            bounded[str(key)] = value
        elif isinstance(value, list):
            bounded[str(key)] = [
                _sanitize_summary(str(item)) if isinstance(item, str) else item
                for item in value[:64]
            ]
        elif isinstance(value, dict):
            nested: dict[str, Any] = {}
            for sub_key, sub_val in list(value.items())[:16]:
                if isinstance(sub_val, str):
                    nested[str(sub_key)] = _sanitize_summary(sub_val)
                elif isinstance(sub_val, (int, float, bool)) or sub_val is None:
                    nested[str(sub_key)] = sub_val
            bounded[str(key)] = nested
        else:
            bounded[str(key)] = _sanitize_summary(str(value))
    encoded = json.dumps(bounded, separators=(",", ":"), sort_keys=True)
    if len(encoded) > _DETAILS_MAX_JSON_BYTES:
        raise SiemEventError("structured_details exceeds size limit")
    assert_payload_safe(bounded)
    return bounded


def deterministic_event_id(*parts: str) -> str:
    key = ":".join(parts)
    return str(uuid.uuid5(_SIEM_ID_NAMESPACE, key))


def category_for_family(family: SiemEventFamily) -> SiemCategory:
    return _FAMILY_CATEGORY[family]


def severity_for_family(family: SiemEventFamily) -> SiemSeverity:
    return _FAMILY_SEVERITY[family]


def priority_class_for(severity: SiemSeverity, family: SiemEventFamily) -> str:
    if severity in (SiemSeverity.CRITICAL, SiemSeverity.HIGH):
        return "critical"
    if family is SiemEventFamily.AGENT_ONLINE:
        return "healthy"
    return "critical" if severity is SiemSeverity.MEDIUM else "healthy"


@dataclass(frozen=True)
class SiemEvent:
    schema_version: int
    event_id: str
    event_family: SiemEventFamily
    severity: SiemSeverity
    category: SiemCategory
    occurred_at: str
    received_at: str
    source_role: str
    summary: str
    agent_id: str | None = None
    run_id: str | None = None
    submission_id: str | None = None
    check_type: str | None = None
    result_state: str | None = None
    structured_details: dict[str, Any] | None = None
    software_version: str | None = None
    protocol_version: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "summary", _sanitize_summary(self.summary))
        if self.schema_version != SIEM_EVENT_SCHEMA_VERSION:
            raise SiemEventError(
                f"unsupported siem_event_schema_version: {self.schema_version}"
            )
        if not _EVENT_ID_RE.fullmatch(self.event_id):
            raise SiemEventError("invalid event_id")
        if self.event_family in NON_EXPORTABLE_FAMILIES:
            raise SiemEventError(
                f"event family {self.event_family.value} is not exportable"
            )
        if self.source_role not in {"standalone", "controller", "all_in_one"}:
            raise SiemEventError("invalid source_role")
        if self.structured_details is not None:
            assert_payload_safe(self.structured_details)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "siem_event_schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_family": self.event_family.value,
            "severity": self.severity.value,
            "category": self.category.value,
            "occurred_at": self.occurred_at,
            "received_at": self.received_at,
            "source_role": self.source_role,
            "summary": self.summary,
        }
        if self.agent_id is not None:
            data["agent_id"] = self.agent_id
        if self.run_id is not None:
            data["run_id"] = self.run_id
        if self.submission_id is not None:
            data["submission_id"] = self.submission_id
        if self.check_type is not None:
            data["check_type"] = self.check_type
        if self.result_state is not None:
            data["result_state"] = self.result_state
        if self.structured_details is not None:
            data["structured_details"] = self.structured_details
        if self.software_version is not None:
            data["software_version"] = self.software_version
        if self.protocol_version is not None:
            data["protocol_version"] = self.protocol_version
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)

    @property
    def priority_class(self) -> str:
        return priority_class_for(self.severity, self.event_family)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _build_event(
    *,
    event_id: str,
    family: SiemEventFamily,
    occurred_at: str,
    received_at: str,
    source_role: str,
    summary: str,
    agent_id: str | None = None,
    run_id: str | None = None,
    submission_id: str | None = None,
    check_type: str | None = None,
    result_state: str | None = None,
    structured_details: dict[str, Any] | None = None,
    software_version: str | None = None,
    protocol_version: int | None = None,
    severity: SiemSeverity | None = None,
) -> SiemEvent:
    if family in NON_EXPORTABLE_FAMILIES:
        raise SiemEventError(f"cannot build exportable event for {family.value}")
    return SiemEvent(
        schema_version=SIEM_EVENT_SCHEMA_VERSION,
        event_id=event_id,
        event_family=family,
        severity=severity or severity_for_family(family),
        category=category_for_family(family),
        occurred_at=occurred_at,
        received_at=received_at,
        source_role=source_role,
        summary=_sanitize_summary(summary),
        agent_id=agent_id,
        run_id=run_id,
        submission_id=submission_id,
        check_type=check_type,
        result_state=result_state,
        structured_details=_sanitize_details(structured_details),
        software_version=software_version,
        protocol_version=protocol_version,
    )


def to_siem_event(
    source: CanonicalEvent | dict[str, Any],
    *,
    source_role: str,
    received_at: str | None = None,
    software_version: str | None = None,
    event_family: SiemEventFamily | None = None,
) -> SiemEvent | None:
    """Convert an internal domain event to a SiemEvent, or None when not exportable."""
    if isinstance(source, CanonicalEvent):
        return _from_canonical_event(
            source,
            source_role=source_role,
            received_at=received_at,
            software_version=software_version,
            event_family=event_family,
        )
    if isinstance(source, dict):
        return _from_mapping(
            source,
            source_role=source_role,
            received_at=received_at,
            software_version=software_version,
            event_family=event_family,
        )
    raise SiemEventError("unsupported source type for to_siem_event")


def _from_canonical_event(
    event: CanonicalEvent,
    *,
    source_role: str,
    received_at: str | None,
    software_version: str | None,
    event_family: SiemEventFamily | None,
) -> SiemEvent | None:
    result_state = event.status.upper()
    try:
        event_type = EventType(event.event_type)
    except ValueError as exc:
        raise SiemEventError(f"unsupported event_type: {event.event_type}") from exc

    family = event_family
    if family is None:
        op = event.payload.get("operational_error")
        if (
            result_state == "ERROR"
            and isinstance(op, dict)
            and str(op.get("kind") or "").strip()
        ):
            # Operational engine failures are not coverage FAILs.
            family = SiemEventFamily.REPOSITORY_UNAVAILABLE
        elif event_type in _EVENT_TYPE_FAMILY:
            if result_state not in _AUDIT_FAILURE_STATUSES:
                return None
            family = _EVENT_TYPE_FAMILY[event_type]
        elif event_type is EventType.FLEET_HEARTBEAT:
            return None
        else:
            return None

    if family is None:
        return None

    summary = _summary_for_audit(event, family)
    details = _details_from_payload(event.payload)
    return _build_event(
        event_id=event.event_id,
        family=family,
        occurred_at=event.occurred_at,
        received_at=received_at or event.received_at or _utc_now_iso(),
        source_role=source_role,
        summary=summary,
        agent_id=event.agent_id,
        run_id=event.run_id,
        submission_id=event.submission_id,
        check_type=_check_type_for_family(family, event_type),
        result_state=result_state,
        structured_details=details,
        software_version=software_version,
    )


def _from_mapping(
    data: dict[str, Any],
    *,
    source_role: str,
    received_at: str | None,
    software_version: str | None,
    event_family: SiemEventFamily | None,
) -> SiemEvent | None:
    kind = str(data.get("kind") or "").strip()
    if event_family is not None:
        family = event_family
    elif kind == "presence_transition":
        raw_family = str(data.get("event_family") or "")
        family = SiemEventFamily(raw_family)
    elif kind == "queue_marker":
        marker = str(data.get("marker") or data.get("queue_marker") or "").upper()
        if marker == "DATA_GAP":
            family = SiemEventFamily.DATA_GAP
        elif marker == "QUEUE_OVERFLOW":
            family = SiemEventFamily.QUEUE_OVERFLOW
        else:
            raise SiemEventError(f"unknown queue marker: {marker}")
    elif kind == "controller_fault":
        family = SiemEventFamily.CONTROLLER_OPERATIONAL_ERROR
    elif kind == "auth_failure":
        family = SiemEventFamily.AGENT_AUTHENTICATION_FAILED
    else:
        raise SiemEventError(f"unknown synthetic event kind: {kind!r}")

    agent_id = data.get("agent_id")
    occurred_at = str(data.get("occurred_at") or _utc_now_iso())
    recv = received_at or str(data.get("received_at") or _utc_now_iso())
    summary = str(data.get("summary") or family.value.replace("_", " "))
    details_raw = data.get("structured_details")
    details = details_raw if isinstance(details_raw, dict) else None

    if family in (SiemEventFamily.AGENT_OFFLINE, SiemEventFamily.AGENT_ONLINE):
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise SiemEventError("presence transition requires agent_id")
        boundary = str(data.get("transition_boundary") or occurred_at)
        event_id = deterministic_event_id(agent_id, family.value, boundary)
    elif family in (SiemEventFamily.DATA_GAP, SiemEventFamily.QUEUE_OVERFLOW):
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise SiemEventError("queue marker requires agent_id")
        first_scan = str(data.get("first_scan_time") or occurred_at)
        last_scan = str(data.get("last_scan_time") or occurred_at)
        event_id = deterministic_event_id(agent_id, family.value, first_scan, last_scan)
    elif family is SiemEventFamily.CONTROLLER_OPERATIONAL_ERROR:
        fault_class = str(data.get("fault_class") or "unknown")
        event_id = deterministic_event_id("controller", fault_class, occurred_at)
    elif family is SiemEventFamily.AGENT_AUTHENTICATION_FAILED:
        event_id = str(data.get("event_id") or deterministic_event_id(
            "auth", str(agent_id or "unknown"), occurred_at
        ))
    else:
        event_id = str(data.get("event_id") or deterministic_event_id(family.value, occurred_at))

    return _build_event(
        event_id=event_id,
        family=family,
        occurred_at=occurred_at,
        received_at=recv,
        source_role=source_role,
        summary=summary,
        agent_id=agent_id if isinstance(agent_id, str) else None,
        run_id=data.get("run_id") if isinstance(data.get("run_id"), str) else None,
        submission_id=(
            data.get("submission_id") if isinstance(data.get("submission_id"), str) else None
        ),
        structured_details=details,
        software_version=software_version,
        protocol_version=(
            int(data["protocol_version"])
            if isinstance(data.get("protocol_version"), int)
            and not isinstance(data.get("protocol_version"), bool)
            else None
        ),
    )


def _check_type_for_event_type(event_type: EventType) -> str | None:
    mapping = {
        EventType.COVERAGE_RESULT: "coverage",
        EventType.INTEGRITY_RESULT: "integrity",
        EventType.RESTORE_RESULT: "restore_verification",
        EventType.AUDIT_COMPLETED: "coverage",
    }
    return mapping.get(event_type)


def _check_type_for_family(family: SiemEventFamily, event_type: EventType) -> str | None:
    if family is SiemEventFamily.REPOSITORY_UNAVAILABLE:
        return "repository"
    return _check_type_for_event_type(event_type)


def _summary_for_audit(event: CanonicalEvent, family: SiemEventFamily) -> str:
    labels = {
        SiemEventFamily.BACKUP_COVERAGE_FAILED: "Backup coverage check failed",
        SiemEventFamily.INTEGRITY_CHECK_FAILED: "Integrity check failed",
        SiemEventFamily.RESTORE_VERIFICATION_FAILED: "Restore verification failed",
        SiemEventFamily.REPOSITORY_UNAVAILABLE: "Backup repository unavailable",
    }
    base = labels.get(family, f"{family.value.replace('_', ' ').title()}")
    agent = event.agent_id or "standalone"
    return f"{base} for {agent} (run {event.run_id})"


def _details_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    assert_payload_safe(payload)
    details: dict[str, Any] = {}
    summary = payload.get("summary")
    if isinstance(summary, dict):
        for key in ("result", "status", "reason"):
            val = summary.get(key)
            if isinstance(val, str) and val.strip():
                details[key] = val.strip()
    for key in ("reason", "error", "check", "result"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            details[key] = val.strip()
    op = payload.get("operational_error")
    if isinstance(op, dict):
        for key in ("engine", "kind", "message"):
            val = op.get(key)
            if isinstance(val, str) and val.strip():
                details[key] = val.strip()
    return details or None


def presence_transition_event(
    *,
    agent_id: str,
    event_family: SiemEventFamily,
    transition_boundary: str,
    source_role: str,
    received_at: str | None = None,
    software_version: str | None = None,
    last_heartbeat: str | None = None,
) -> SiemEvent:
    """Build a presence transition SiemEvent with deterministic identity."""
    if event_family not in (SiemEventFamily.AGENT_OFFLINE, SiemEventFamily.AGENT_ONLINE):
        raise SiemEventError("presence_transition_event requires AGENT_OFFLINE or AGENT_ONLINE")
    summary = (
        f"Agent {agent_id} is offline"
        if event_family is SiemEventFamily.AGENT_OFFLINE
        else f"Agent {agent_id} is online"
    )
    details: dict[str, Any] | None = None
    if last_heartbeat:
        details = {"last_heartbeat": last_heartbeat}
    return _build_event(
        event_id=deterministic_event_id(agent_id, event_family.value, transition_boundary),
        family=event_family,
        occurred_at=transition_boundary,
        received_at=received_at or _utc_now_iso(),
        source_role=source_role,
        summary=summary,
        agent_id=agent_id,
        structured_details=details,
        software_version=software_version,
    )


def audit_result_to_siem_event(
    result: dict[str, Any],
    *,
    occurred_at: str,
    source_role: str,
    run_id: str | None = None,
    event_id: str | None = None,
    received_at: str | None = None,
    software_version: str | None = None,
    event_type: EventType = EventType.AUDIT_COMPLETED,
) -> SiemEvent | None:
    """Convert a local audit result dict to SiemEvent when it represents a failure."""
    from backuplint.events import event_from_audit_result

    canonical = event_from_audit_result(
        result,
        occurred_at=occurred_at,
        run_id=run_id,
        received_at=received_at,
        event_type=event_type,
        event_id=event_id,
    )
    state = status_from_audit_result(result)
    if state not in _AUDIT_FAILURE_STATUSES:
        return None
    return to_siem_event(
        canonical,
        source_role=source_role,
        received_at=received_at,
        software_version=software_version,
    )
