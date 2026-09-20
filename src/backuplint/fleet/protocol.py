"""BackupLint fleet protocol constants and envelope helpers (v0.5)."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from backuplint.fleet.compat import (
    CURRENT_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    check_protocol_version,
)

MAX_BODY_BYTES = 256_000
MAX_CSR_PEM_BYTES = 32_768
MAX_HOSTNAME_CHARS = 255
MAX_ENROLL_LABEL_CHARS = 128

_AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9._-]{8,128}$")
# Hostnames/labels: printable ASCII without control chars or whitespace runs.
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,253}[A-Za-z0-9])?$")

__all__ = [
    "CURRENT_PROTOCOL_VERSION",
    "MAX_BODY_BYTES",
    "MAX_CSR_PEM_BYTES",
    "MAX_ENROLL_LABEL_CHARS",
    "MAX_HOSTNAME_CHARS",
    "PROTOCOL_VERSION",
    "ProtocolError",
    "ResultEnvelope",
    "is_valid_agent_id",
    "new_agent_id",
    "new_submission_id",
    "parse_envelope",
    "utc_now_iso",
    "validate_enroll_hostname",
]


def is_valid_agent_id(agent_id: str) -> bool:
    return bool(_AGENT_ID_RE.fullmatch(agent_id))


def validate_enroll_hostname(hostname: str) -> str:
    """Return a normalized hostname or raise ProtocolError."""
    if not isinstance(hostname, str):
        raise ProtocolError("hostname required")
    value = hostname.strip()
    if not value:
        raise ProtocolError("hostname required")
    if len(value) > MAX_HOSTNAME_CHARS:
        raise ProtocolError("hostname too long")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ProtocolError("hostname contains control characters")
    if not _HOSTNAME_RE.fullmatch(value):
        raise ProtocolError("hostname has invalid characters")
    return value


class ProtocolError(Exception):
    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def new_submission_id() -> str:
    return str(uuid.uuid4())


def new_agent_id() -> str:
    return f"agent-{uuid.uuid4().hex}"


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ResultEnvelope:
    agent_id: str
    submission_id: str
    scan_time: str
    backuplint_version: str
    platform: str
    result: dict[str, Any]
    protocol_version: int = PROTOCOL_VERSION
    run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "protocol_version": self.protocol_version,
            "agent_id": self.agent_id,
            "submission_id": self.submission_id,
            "scan_time": self.scan_time,
            "backuplint_version": self.backuplint_version,
            "platform": self.platform,
            "result": self.result,
        }
        if self.run_id:
            data["run_id"] = self.run_id
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)


def parse_envelope(data: object) -> ResultEnvelope:
    if not isinstance(data, dict):
        raise ProtocolError("envelope must be a JSON object", code="PROTOCOL_SCHEMA")
    required = {
        "protocol_version",
        "agent_id",
        "submission_id",
        "scan_time",
        "backuplint_version",
        "platform",
        "result",
    }
    missing = sorted(required - set(data))
    if missing:
        raise ProtocolError(
            f"envelope missing fields: {', '.join(missing)}", code="PROTOCOL_SCHEMA"
        )
    unknown = sorted(set(data) - required - {"received_time", "run_id"})
    if unknown:
        raise ProtocolError(
            f"envelope unknown fields: {', '.join(unknown)}", code="PROTOCOL_SCHEMA"
        )
    compat = check_protocol_version(data.get("protocol_version"))
    if not compat.ok:
        raise ProtocolError(compat.message, code=str(compat.code))
    version = compat.agent_protocol
    if version is None:
        raise ProtocolError(
            "protocol_version must be an integer",
            code="PROTOCOL_MALFORMED",
        )
    agent_id = data["agent_id"]
    if not isinstance(agent_id, str) or not _AGENT_ID_RE.fullmatch(agent_id):
        raise ProtocolError("invalid agent_id", code="PROTOCOL_SCHEMA")
    submission_id = data["submission_id"]
    if not isinstance(submission_id, str) or not submission_id.strip():
        raise ProtocolError("invalid submission_id", code="PROTOCOL_SCHEMA")
    for key in ("scan_time", "backuplint_version", "platform"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise ProtocolError(f"invalid {key}", code="PROTOCOL_SCHEMA")
    result = data["result"]
    if not isinstance(result, dict):
        raise ProtocolError("result must be an object", code="PROTOCOL_SCHEMA")
    # Reject obvious secret-bearing keys in the nested result.
    forbidden = {"password", "password_file", "secret", "token", "private_key"}
    flat_keys = {str(k).lower() for k in result}
    if flat_keys & forbidden:
        raise ProtocolError(
            "result contains forbidden secret-bearing keys", code="PROTOCOL_SCHEMA"
        )
    run_id_raw = data.get("run_id")
    run_id: str | None = None
    if run_id_raw is not None:
        if not isinstance(run_id_raw, str) or not run_id_raw.strip():
            raise ProtocolError("invalid run_id", code="PROTOCOL_SCHEMA")
        run_id = run_id_raw.strip()
    return ResultEnvelope(
        protocol_version=version,
        agent_id=agent_id,
        submission_id=submission_id.strip(),
        scan_time=data["scan_time"].strip(),
        backuplint_version=data["backuplint_version"].strip(),
        platform=data["platform"].strip(),
        result=result,
        run_id=run_id,
    )
