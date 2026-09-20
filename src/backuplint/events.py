"""Versioned canonical event model for BackupLint results (v0.5 stabilization).

Future dashboard / SIEM / API consumers should prefer this representation over
reinterpretating raw scan JSON. Wire protocol envelopes remain compatible;
controllers normalize envelopes into events on ingest.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

EVENT_SCHEMA_VERSION = 1

_EVENT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_RUN_ID_RE = re.compile(r"^(run-[a-zA-Z0-9._-]{8,128}|legacy-[a-zA-Z0-9._-]{8,128})$")

_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "password",
        "password_file",
        "secret",
        "token",
        "private_key",
        "enrollment_token",
        "aws_secret_access_key",
        "restic_password",
    }
)


class EventError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class EventType(StrEnum):
    AUDIT_COMPLETED = "audit.completed"
    COVERAGE_RESULT = "coverage.result"
    INTEGRITY_RESULT = "integrity.result"
    RESTORE_RESULT = "restore.result"
    SCHEDULE_RUN = "schedule.run"
    FLEET_HEARTBEAT = "fleet.heartbeat"
    FLEET_SYNC = "fleet.sync"
    AGENT_ENROLLED = "agent.enrolled"
    AGENT_REVOKED = "agent.revoked"


def new_event_id() -> str:
    return str(uuid.uuid4())


def new_run_id() -> str:
    return f"run-{uuid.uuid4().hex}"


def legacy_run_id(submission_id: str) -> str:
    """Stable run correlation for pre-stabilization submissions."""
    return f"legacy-{submission_id.strip()}"


@dataclass(frozen=True)
class CanonicalEvent:
    """Versioned internal event. `payload` must not contain secrets."""

    schema_version: int
    event_id: str
    event_type: str
    run_id: str
    occurred_at: str
    status: str
    payload: dict[str, Any]
    agent_id: str | None = None
    received_at: str | None = None
    submission_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "run_id": self.run_id,
            "occurred_at": self.occurred_at,
            "status": self.status,
            "payload": self.payload,
        }
        if self.agent_id is not None:
            data["agent_id"] = self.agent_id
        if self.received_at is not None:
            data["received_at"] = self.received_at
        if self.submission_id is not None:
            data["submission_id"] = self.submission_id
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)


def assert_payload_safe(payload: dict[str, Any]) -> None:
    flat = {str(k).lower() for k in payload}
    if flat & _FORBIDDEN_PAYLOAD_KEYS:
        raise EventError("event payload contains forbidden secret-bearing keys")


def parse_event(data: object) -> CanonicalEvent:
    if not isinstance(data, dict):
        raise EventError("event must be a JSON object")
    if "schema_version" not in data:
        raise EventError("event missing schema_version")
    version = data["schema_version"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise EventError("schema_version must be an integer")
    if version != EVENT_SCHEMA_VERSION:
        raise EventError(f"unsupported event schema_version: {version}")

    required = {
        "schema_version",
        "event_id",
        "event_type",
        "run_id",
        "occurred_at",
        "status",
        "payload",
    }
    missing = sorted(required - set(data))
    if missing:
        raise EventError(f"event missing fields: {', '.join(missing)}")
    allowed = required | {"agent_id", "received_at", "submission_id"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise EventError(f"event unknown fields: {', '.join(unknown)}")

    event_id = data["event_id"]
    if not isinstance(event_id, str) or not _EVENT_ID_RE.fullmatch(event_id):
        raise EventError("invalid event_id")
    event_type = data["event_type"]
    if not isinstance(event_type, str) or not event_type.strip():
        raise EventError("invalid event_type")
    try:
        EventType(event_type)
    except ValueError as exc:
        raise EventError(f"unsupported event_type: {event_type}") from exc
    run_id = data["run_id"]
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise EventError("invalid run_id")
    for key in ("occurred_at", "status"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise EventError(f"invalid {key}")
    payload = data["payload"]
    if not isinstance(payload, dict):
        raise EventError("payload must be an object")
    assert_payload_safe(payload)

    agent_id = data.get("agent_id")
    if agent_id is not None and (
        not isinstance(agent_id, str) or not agent_id.strip()
    ):
        raise EventError("invalid agent_id")
    received_at = data.get("received_at")
    if received_at is not None and (
        not isinstance(received_at, str) or not received_at.strip()
    ):
        raise EventError("invalid received_at")
    submission_id = data.get("submission_id")
    if submission_id is not None and (
        not isinstance(submission_id, str) or not submission_id.strip()
    ):
        raise EventError("invalid submission_id")

    return CanonicalEvent(
        schema_version=version,
        event_id=event_id.strip(),
        event_type=event_type.strip(),
        run_id=run_id.strip(),
        occurred_at=data["occurred_at"].strip(),
        status=data["status"].strip(),
        payload=payload,
        agent_id=agent_id.strip() if isinstance(agent_id, str) else None,
        received_at=received_at.strip() if isinstance(received_at, str) else None,
        submission_id=(
            submission_id.strip() if isinstance(submission_id, str) else None
        ),
    )


def status_from_audit_result(result: dict[str, Any]) -> str:
    summary = result.get("summary")
    if isinstance(summary, dict):
        raw = summary.get("result")
        if isinstance(raw, str) and raw.strip():
            return raw.strip().upper()
    raw = result.get("result")
    if isinstance(raw, str) and raw.strip():
        return raw.strip().upper()
    return "UNKNOWN"


def event_from_audit_result(
    result: dict[str, Any],
    *,
    occurred_at: str,
    run_id: str | None = None,
    agent_id: str | None = None,
    submission_id: str | None = None,
    received_at: str | None = None,
    event_type: EventType = EventType.AUDIT_COMPLETED,
    event_id: str | None = None,
) -> CanonicalEvent:
    assert_payload_safe(result)
    return CanonicalEvent(
        schema_version=EVENT_SCHEMA_VERSION,
        event_id=event_id or new_event_id(),
        event_type=event_type.value,
        run_id=run_id or new_run_id(),
        occurred_at=occurred_at,
        status=status_from_audit_result(result),
        payload=result,
        agent_id=agent_id,
        received_at=received_at,
        submission_id=submission_id,
    )


def event_is_stale(
    occurred_at: str,
    *,
    now: datetime | None = None,
    max_age: timedelta,
) -> bool:
    """True when ``occurred_at`` is older than ``max_age`` relative to ``now``.

    An old PASS must not be treated as current health solely because it was
    stored successfully.
    """
    if max_age.total_seconds() < 0:
        raise EventError("max_age must be non-negative")
    now = now or datetime.now(UTC)
    try:
        occurred = datetime.fromisoformat(occurred_at)
    except ValueError as exc:
        raise EventError(f"invalid occurred_at: {occurred_at}") from exc
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=UTC)
    return (now - occurred) > max_age
