"""SIEM event conversion and schema tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backuplint.events import (
    EventError,
    EventType,
    event_from_audit_result,
    new_event_id,
    new_run_id,
)
from backuplint.siem.event import (
    NON_EXPORTABLE_FAMILIES,
    SIEM_EVENT_SCHEMA_VERSION,
    SiemCategory,
    SiemEvent,
    SiemEventError,
    SiemEventFamily,
    SiemSeverity,
    audit_result_to_siem_event,
    category_for_family,
    deterministic_event_id,
    presence_transition_event,
    severity_for_family,
    to_siem_event,
)


def test_schema_constants_and_non_exportable_guard() -> None:
    assert SIEM_EVENT_SCHEMA_VERSION == 1
    assert SiemEventFamily.SIEM_DELIVERY_ERROR in NON_EXPORTABLE_FAMILIES


@pytest.mark.parametrize(
    ("family", "severity", "category"),
    [
        (SiemEventFamily.BACKUP_COVERAGE_FAILED, SiemSeverity.HIGH, SiemCategory.BACKUP_ASSURANCE),
        (SiemEventFamily.INTEGRITY_CHECK_FAILED, SiemSeverity.HIGH, SiemCategory.BACKUP_ASSURANCE),
        (
            SiemEventFamily.RESTORE_VERIFICATION_FAILED,
            SiemSeverity.HIGH,
            SiemCategory.BACKUP_ASSURANCE,
        ),
        (SiemEventFamily.AGENT_OFFLINE, SiemSeverity.MEDIUM, SiemCategory.FLEET_TRANSPORT),
        (SiemEventFamily.AGENT_ONLINE, SiemSeverity.LOW, SiemCategory.FLEET_TRANSPORT),
        (SiemEventFamily.DATA_GAP, SiemSeverity.HIGH, SiemCategory.FLEET_TRANSPORT),
        (SiemEventFamily.QUEUE_OVERFLOW, SiemSeverity.CRITICAL, SiemCategory.FLEET_TRANSPORT),
        (
            SiemEventFamily.AGENT_AUTHENTICATION_FAILED,
            SiemSeverity.CRITICAL,
            SiemCategory.SECURITY_AUDIT,
        ),
        (
            SiemEventFamily.CONTROLLER_OPERATIONAL_ERROR,
            SiemSeverity.LOW,
            SiemCategory.CONTROLLER_OPERATIONAL,
        ),
    ],
)
def test_family_metadata(family, severity, category) -> None:
    assert severity_for_family(family) is severity
    assert category_for_family(family) is category


def test_canonical_failure_conversion() -> None:
    canonical = event_from_audit_result(
        {"summary": {"result": "FAIL"}, "reason": "missing snapshot"},
        occurred_at="2026-09-15T12:00:00+00:00",
        run_id=new_run_id(),
        agent_id="agent-1",
        event_type=EventType.COVERAGE_RESULT,
    )
    siem = to_siem_event(canonical, source_role="controller")
    assert siem is not None
    assert siem.event_id == canonical.event_id
    assert siem.event_family is SiemEventFamily.BACKUP_COVERAGE_FAILED
    assert siem.severity is SiemSeverity.HIGH
    assert siem.category is SiemCategory.BACKUP_ASSURANCE
    assert siem.result_state == "FAIL"
    assert "agent-1" in siem.summary


def test_pass_results_not_exported() -> None:
    canonical = event_from_audit_result(
        {"summary": {"result": "PASS"}},
        occurred_at="2026-09-15T12:00:00+00:00",
        event_type=EventType.COVERAGE_RESULT,
    )
    assert to_siem_event(canonical, source_role="standalone") is None


def test_forbidden_payload_keys_rejected() -> None:
    with pytest.raises(EventError):
        event_from_audit_result(
            {"summary": {"result": "FAIL"}, "token": "secret-value"},
            occurred_at="2026-09-15T12:00:00+00:00",
            event_type=EventType.INTEGRITY_RESULT,
        )


def test_deterministic_event_id_stable() -> None:
    first = deterministic_event_id("agent-1", "agent_offline", "2026-09-15T12:00:00+00:00")
    second = deterministic_event_id("agent-1", "agent_offline", "2026-09-15T12:00:00+00:00")
    assert first == second


def test_presence_transition_event() -> None:
    event = presence_transition_event(
        agent_id="agent-42",
        event_family=SiemEventFamily.AGENT_OFFLINE,
        transition_boundary="2026-09-15T12:00:00+00:00",
        source_role="controller",
        last_heartbeat="2026-09-15T11:00:00+00:00",
    )
    assert event.priority_class == "critical"
    assert event.structured_details == {"last_heartbeat": "2026-09-15T11:00:00+00:00"}


def test_synthetic_queue_marker_mapping() -> None:
    event = to_siem_event(
        {
            "kind": "queue_marker",
            "marker": "DATA_GAP",
            "agent_id": "agent-7",
            "occurred_at": "2026-09-15T10:00:00+00:00",
            "first_scan_time": "2026-09-15T09:00:00+00:00",
            "last_scan_time": "2026-09-15T10:00:00+00:00",
            "summary": "Fleet queue data gap",
        },
        source_role="controller",
    )
    assert event is not None
    assert event.event_family is SiemEventFamily.DATA_GAP
    assert event.severity is SiemSeverity.HIGH


def test_siem_event_json_omits_null_optional_fields() -> None:
    event = SiemEvent(
        schema_version=SIEM_EVENT_SCHEMA_VERSION,
        event_id=new_event_id(),
        event_family=SiemEventFamily.AGENT_ONLINE,
        severity=SiemSeverity.LOW,
        category=SiemCategory.FLEET_TRANSPORT,
        occurred_at=datetime.now(UTC).isoformat(),
        received_at=datetime.now(UTC).isoformat(),
        source_role="controller",
        summary="Agent online",
        agent_id="agent-1",
    )
    payload = event.to_dict()
    assert "run_id" not in payload
    assert payload["agent_id"] == "agent-1"


def test_audit_result_helper_exports_failures_only() -> None:
    exported = audit_result_to_siem_event(
        {"summary": {"result": "ERROR"}},
        occurred_at="2026-09-15T12:00:00+00:00",
        source_role="standalone",
    )
    assert exported is not None
    assert exported.source_role == "standalone"
    skipped = audit_result_to_siem_event(
        {"summary": {"result": "PASS"}},
        occurred_at="2026-09-15T12:00:00+00:00",
        source_role="standalone",
    )
    assert skipped is None


def test_control_chars_stripped_from_summary() -> None:
    event = SiemEvent(
        schema_version=SIEM_EVENT_SCHEMA_VERSION,
        event_id=new_event_id(),
        event_family=SiemEventFamily.CONTROLLER_OPERATIONAL_ERROR,
        severity=SiemSeverity.LOW,
        category=SiemCategory.CONTROLLER_OPERATIONAL,
        occurred_at=datetime.now(UTC).isoformat(),
        received_at=datetime.now(UTC).isoformat(),
        source_role="controller",
        summary="line1\nline2\rinject",
    )
    assert "\n" not in event.summary
    assert "\r" not in event.summary


def test_building_siem_delivery_error_raises() -> None:
    with pytest.raises(SiemEventError):
        SiemEvent(
            schema_version=SIEM_EVENT_SCHEMA_VERSION,
            event_id=new_event_id(),
            event_family=SiemEventFamily.SIEM_DELIVERY_ERROR,
            severity=SiemSeverity.INFO,
            category=SiemCategory.SIEM_DELIVERY,
            occurred_at=datetime.now(UTC).isoformat(),
            received_at=datetime.now(UTC).isoformat(),
            source_role="controller",
            summary="delivery recovered",
        )
