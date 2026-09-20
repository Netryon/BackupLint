"""Central fleet protocol version negotiation / compatibility policy.

Controller accepts the current protocol and the immediately previous
protocol for rolling upgrades. Unknown newer or older-than-window
versions are rejected with stable machine-readable error codes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Wire protocol: current agents speak 2; immediately previous (1) remains
# accepted so mixed fleets can roll. Do not silently remap unknown versions.
CURRENT_PROTOCOL_VERSION = 2
PREVIOUS_PROTOCOL_VERSION = 1
MIN_SUPPORTED_PROTOCOL_VERSION = PREVIOUS_PROTOCOL_VERSION
MAX_SUPPORTED_PROTOCOL_VERSION = CURRENT_PROTOCOL_VERSION

# Alias used across the codebase for "what this build speaks".
PROTOCOL_VERSION = CURRENT_PROTOCOL_VERSION


class ProtocolCompatCode(StrEnum):
    OK = "PROTOCOL_OK"
    TOO_OLD = "PROTOCOL_UNSUPPORTED_TOO_OLD"
    TOO_NEW = "PROTOCOL_UNSUPPORTED_TOO_NEW"
    MALFORMED = "PROTOCOL_MALFORMED"


@dataclass(frozen=True)
class ProtocolCompatResult:
    ok: bool
    code: ProtocolCompatCode
    message: str
    agent_protocol: int | None = None
    controller_current: int = CURRENT_PROTOCOL_VERSION
    controller_min: int = MIN_SUPPORTED_PROTOCOL_VERSION
    controller_max: int = MAX_SUPPORTED_PROTOCOL_VERSION

    def to_error_body(self) -> dict[str, object]:
        return {
            "error": self.message,
            "error_code": str(self.code),
            "agent_protocol_version": self.agent_protocol,
            "controller_current_protocol": self.controller_current,
            "controller_supported_min": self.controller_min,
            "controller_supported_max": self.controller_max,
        }


def check_protocol_version(version: object) -> ProtocolCompatResult:
    """Evaluate an agent-reported protocol_version against controller policy."""
    if isinstance(version, bool) or not isinstance(version, int):
        return ProtocolCompatResult(
            ok=False,
            code=ProtocolCompatCode.MALFORMED,
            message="protocol_version must be an integer",
            agent_protocol=None,
        )
    if version < MIN_SUPPORTED_PROTOCOL_VERSION:
        return ProtocolCompatResult(
            ok=False,
            code=ProtocolCompatCode.TOO_OLD,
            message=(
                f"protocol_version {version} is older than supported window "
                f"[{MIN_SUPPORTED_PROTOCOL_VERSION}, {MAX_SUPPORTED_PROTOCOL_VERSION}]; "
                "agent upgrade required"
            ),
            agent_protocol=version,
        )
    if version > MAX_SUPPORTED_PROTOCOL_VERSION:
        return ProtocolCompatResult(
            ok=False,
            code=ProtocolCompatCode.TOO_NEW,
            message=(
                f"protocol_version {version} is newer than this controller supports "
                f"(current={CURRENT_PROTOCOL_VERSION}); controller upgrade required"
            ),
            agent_protocol=version,
        )
    return ProtocolCompatResult(
        ok=True,
        code=ProtocolCompatCode.OK,
        message="compatible",
        agent_protocol=version,
    )


def supported_protocol_versions() -> tuple[int, ...]:
    return tuple(
        range(MIN_SUPPORTED_PROTOCOL_VERSION, MAX_SUPPORTED_PROTOCOL_VERSION + 1)
    )


def health_protocol_payload(*, software_version: str) -> dict[str, object]:
    """Stable health fields for agents and operators."""
    return {
        "ok": True,
        "protocol_version": CURRENT_PROTOCOL_VERSION,
        "software_version": software_version,
        "supported_protocol_versions": list(supported_protocol_versions()),
        "previous_protocol_version": PREVIOUS_PROTOCOL_VERSION,
    }
