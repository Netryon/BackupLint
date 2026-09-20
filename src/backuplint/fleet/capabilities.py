"""Structured agent capability reporting (fleet foundations).

Capabilities describe what is installed / supported / available on an agent.
They are **not** audit check outcomes: NOT_INSTALLED / UNSUPPORTED / UNAVAILABLE
must never be rewritten as FAIL, and AVAILABLE does not imply PASS.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

CAPABILITIES_SCHEMA_VERSION = 1


class CapabilityId(StrEnum):
    COVERAGE_SCANNING = "coverage_scanning"
    DOCKER_COMPOSE = "docker_compose"
    RESTIC = "restic"
    STANDARD_INTEGRITY = "standard_integrity"
    DEEP_INTEGRITY = "deep_integrity"
    RESTORE_VERIFICATION = "restore_verification"
    SCHEDULER = "scheduler"
    FLEET_REPORTING = "fleet_reporting"


class CapabilityReason(StrEnum):
    OK = "OK"
    BINARY_MISSING = "BINARY_MISSING"
    MODULE_MISSING = "MODULE_MISSING"
    RESTIC_UNAVAILABLE = "RESTIC_UNAVAILABLE"
    DOCKER_UNAVAILABLE = "DOCKER_UNAVAILABLE"
    PLATFORM_UNSUPPORTED = "PLATFORM_UNSUPPORTED"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    NOT_CONFIGURED = "NOT_CONFIGURED"


@dataclass(frozen=True, slots=True)
class CapabilityState:
    """Tri-state model for one capability family."""

    installed: bool
    supported: bool
    available: bool
    reason: str = CapabilityReason.OK

    def to_dict(self) -> dict[str, object]:
        return {
            "installed": self.installed,
            "supported": self.supported,
            "available": self.available,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: object) -> CapabilityState:
        if not isinstance(data, dict):
            raise ValueError("capability must be an object")
        installed = bool(data.get("installed", False))
        supported = bool(data.get("supported", False))
        available = bool(data.get("available", False))
        reason = str(data.get("reason") or CapabilityReason.OK)
        # Available requires installed+supported; never invent success.
        if available and not (installed and supported):
            available = False
            if reason == CapabilityReason.OK:
                reason = CapabilityReason.NOT_CONFIGURED
        return cls(
            installed=installed,
            supported=supported,
            available=available,
            reason=reason,
        )


def _which(name: str) -> bool:
    return shutil.which(name) is not None


def _module_importable(name: str) -> bool:
    try:
        __import__(name)
    except Exception:  # noqa: BLE001 — capability probe must not crash agent
        return False
    return True


def _detect_deployment_form() -> str:
    """Best-effort deployment form; not a security boundary."""
    if Path("/.dockerenv").exists():
        return "container"
    if os.environ.get("BACKUP_LINT_DEPLOYMENT_FORM"):
        return str(os.environ["BACKUP_LINT_DEPLOYMENT_FORM"])
    return "native"


def _state(
    *,
    installed: bool,
    supported: bool,
    available: bool,
    reason: str,
) -> CapabilityState:
    return CapabilityState.from_dict(
        {
            "installed": installed,
            "supported": supported,
            "available": available,
            "reason": reason,
        }
    )


def collect_local_capabilities(
    *,
    restic_available: bool | None = None,
    docker_available: bool | None = None,
) -> dict[str, Any]:
    """Probe local host and build a versioned capabilities document."""
    docker_ok = _which("docker") if docker_available is None else docker_available
    restic_ok = _which("restic") if restic_available is None else restic_available
    compose_mod = _module_importable("backuplint.compose")
    coverage_mod = _module_importable("backuplint.coverage")
    restic_mod = _module_importable("backuplint.restic")
    scheduler_mod = _module_importable("backuplint.scheduler")
    fleet_mod = _module_importable("backuplint.fleet.agent")

    caps: dict[str, CapabilityState] = {
        CapabilityId.COVERAGE_SCANNING: _state(
            installed=coverage_mod and compose_mod,
            supported=True,
            available=coverage_mod and compose_mod and docker_ok,
            reason=(
                CapabilityReason.OK
                if coverage_mod and compose_mod and docker_ok
                else (
                    CapabilityReason.DOCKER_UNAVAILABLE
                    if not docker_ok
                    else CapabilityReason.MODULE_MISSING
                )
            ),
        ),
        CapabilityId.DOCKER_COMPOSE: _state(
            installed=docker_ok,
            supported=True,
            available=docker_ok,
            reason=(
                CapabilityReason.OK if docker_ok else CapabilityReason.DOCKER_UNAVAILABLE
            ),
        ),
        CapabilityId.RESTIC: _state(
            installed=restic_ok,
            supported=True,
            available=restic_ok,
            reason=(
                CapabilityReason.OK
                if restic_ok
                else CapabilityReason.RESTIC_UNAVAILABLE
            ),
        ),
        CapabilityId.STANDARD_INTEGRITY: _state(
            installed=restic_mod,
            supported=True,
            available=restic_mod and restic_ok,
            reason=(
                CapabilityReason.OK
                if restic_mod and restic_ok
                else (
                    CapabilityReason.RESTIC_UNAVAILABLE
                    if not restic_ok
                    else CapabilityReason.MODULE_MISSING
                )
            ),
        ),
        CapabilityId.DEEP_INTEGRITY: _state(
            installed=restic_mod,
            supported=True,
            available=restic_mod and restic_ok,
            reason=(
                CapabilityReason.OK
                if restic_mod and restic_ok
                else (
                    CapabilityReason.RESTIC_UNAVAILABLE
                    if not restic_ok
                    else CapabilityReason.MODULE_MISSING
                )
            ),
        ),
        CapabilityId.RESTORE_VERIFICATION: _state(
            installed=restic_mod,
            supported=True,
            available=restic_mod and restic_ok,
            reason=(
                CapabilityReason.OK
                if restic_mod and restic_ok
                else CapabilityReason.RESTIC_UNAVAILABLE
            ),
        ),
        CapabilityId.SCHEDULER: _state(
            installed=scheduler_mod,
            supported=True,
            available=scheduler_mod,
            reason=(
                CapabilityReason.OK
                if scheduler_mod
                else CapabilityReason.MODULE_MISSING
            ),
        ),
        CapabilityId.FLEET_REPORTING: _state(
            installed=fleet_mod,
            supported=True,
            available=fleet_mod,
            reason=CapabilityReason.OK if fleet_mod else CapabilityReason.MODULE_MISSING,
        ),
    }
    return {
        "schema_version": CAPABILITIES_SCHEMA_VERSION,
        "deployment_form": _detect_deployment_form(),
        "capabilities": {cid.value: state.to_dict() for cid, state in caps.items()},
    }


def validate_capabilities_document(data: object) -> dict[str, Any]:
    """Validate/normalize a capabilities document from an agent.

    Unknown capability keys from newer agents are preserved (forward-compatible).
    Malformed entries are rejected.
    """
    if not isinstance(data, dict):
        raise ValueError("capabilities document must be an object")
    schema = data.get("schema_version", CAPABILITIES_SCHEMA_VERSION)
    if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
        raise ValueError("capabilities.schema_version must be a positive integer")
    if schema > CAPABILITIES_SCHEMA_VERSION + 1:
        # Newer-than-next is accepted structurally but not interpreted deeply.
        pass
    caps_raw = data.get("capabilities")
    if not isinstance(caps_raw, dict):
        raise ValueError("capabilities.capabilities must be an object")
    normalized: dict[str, object] = {}
    for key, value in caps_raw.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("capability key must be a non-empty string")
        normalized[key.strip()] = CapabilityState.from_dict(value).to_dict()
    deployment = data.get("deployment_form", "unknown")
    if not isinstance(deployment, str) or not deployment.strip():
        deployment = "unknown"
    return {
        "schema_version": schema,
        "deployment_form": deployment.strip(),
        "capabilities": normalized,
    }


def capabilities_to_json(doc: dict[str, Any]) -> str:
    return json.dumps(doc, separators=(",", ":"), sort_keys=True)


# Explicit: capability unavailability is not an audit FAIL.
AUDIT_FAIL_STATUSES = frozenset({"FAIL", "ERROR"})
CAPABILITY_NON_FAIL_REASONS = frozenset(item.value for item in CapabilityReason)
