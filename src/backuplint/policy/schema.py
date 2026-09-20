"""Policy snapshot schema, canonical serialization, and SHA-256 hashing."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from backuplint.policy.errors import PolicyError
from backuplint.policy.settings import validate_policy_settings

POLICY_SCHEMA_VERSION = 1
MAX_POLICY_BYTES = 256 * 1024
MAX_DISPLAY_NAME_CHARS = 256
MAX_DESCRIPTION_CHARS = 4096
MAX_ID_CHARS = 128


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    schema_version: int
    policy_id: str
    revision_id: str
    created_at: str
    created_by: str
    content_sha256: str
    display_name: str
    description: str
    settings: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return snapshot_to_dict(self)

    def canonical_body(self) -> dict[str, object]:
        """Fields included in content hash (excludes content_sha256)."""
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "revision_id": self.revision_id,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "display_name": self.display_name,
            "description": self.description,
            "settings": self.settings,
        }


def new_policy_id() -> str:
    return f"pol-{uuid.uuid4().hex[:16]}"


def new_revision_id() -> str:
    return f"rev-{secrets.token_hex(8)}"


def canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_sha256(payload: dict[str, object]) -> str:
    raw = canonical_json(payload).encode("utf-8")
    if len(raw) > MAX_POLICY_BYTES:
        raise PolicyError(f"policy exceeds {MAX_POLICY_BYTES} bytes")
    return hashlib.sha256(raw).hexdigest()


def snapshot_to_dict(snapshot: PolicySnapshot) -> dict[str, object]:
    body = snapshot.canonical_body()
    body["content_sha256"] = snapshot.content_sha256
    return body


def _validate_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyError(f"{field} must be a non-empty string")
    text = value.strip()
    if len(text) > MAX_ID_CHARS:
        raise PolicyError(f"{field} too long")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise PolicyError(f"{field} contains control characters")
    return text


def _validate_text(value: object, field: str, *, max_chars: int) -> str:
    if not isinstance(value, str):
        raise PolicyError(f"{field} must be a string")
    if len(value) > max_chars:
        raise PolicyError(f"{field} exceeds {max_chars} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise PolicyError(f"{field} contains control characters")
    return value


def build_policy_snapshot(
    *,
    policy_id: str,
    revision_id: str | None = None,
    created_by: str,
    display_name: str,
    description: str = "",
    settings: dict[str, object],
    created_at: str | None = None,
) -> PolicySnapshot:
    resolved_policy_id = _validate_id(policy_id, "policy_id")
    resolved_revision_id = _validate_id(revision_id or new_revision_id(), "revision_id")
    resolved_display = _validate_text(
        display_name, "display_name", max_chars=MAX_DISPLAY_NAME_CHARS
    )
    resolved_description = _validate_text(
        description, "description", max_chars=MAX_DESCRIPTION_CHARS
    )
    if not isinstance(settings, dict):
        raise PolicyError("settings must be an object")
    validated_settings = validate_policy_settings(settings)
    resolved_created_at = created_at or datetime.now(UTC).isoformat()
    body: dict[str, object] = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "policy_id": resolved_policy_id,
        "revision_id": resolved_revision_id,
        "created_at": resolved_created_at,
        "created_by": str(created_by or "unknown"),
        "display_name": resolved_display,
        "description": resolved_description,
        "settings": validated_settings,
    }
    digest = content_sha256(body)
    return PolicySnapshot(
        schema_version=POLICY_SCHEMA_VERSION,
        policy_id=resolved_policy_id,
        revision_id=resolved_revision_id,
        created_at=resolved_created_at,
        created_by=str(created_by or "unknown"),
        content_sha256=digest,
        display_name=resolved_display,
        description=resolved_description,
        settings=validated_settings,
    )


def parse_policy_snapshot(data: object, *, verify_hash: bool = True) -> PolicySnapshot:
    if not isinstance(data, dict):
        raise PolicyError("policy must be an object")
    schema_version = data.get("schema_version")
    if schema_version != POLICY_SCHEMA_VERSION:
        raise PolicyError(
            f"unsupported policy schema_version {schema_version!r}; "
            f"expected {POLICY_SCHEMA_VERSION}"
        )
    policy_id = _validate_id(data.get("policy_id"), "policy_id")
    revision_id = _validate_id(data.get("revision_id"), "revision_id")
    created_at = _validate_text(str(data.get("created_at") or ""), "created_at", max_chars=64)
    created_by = _validate_text(
        str(data.get("created_by") or "unknown"), "created_by", max_chars=256
    )
    display_name = _validate_text(
        str(data.get("display_name") or ""), "display_name", max_chars=MAX_DISPLAY_NAME_CHARS
    )
    description = _validate_text(
        str(data.get("description") or ""), "description", max_chars=MAX_DESCRIPTION_CHARS
    )
    settings_raw = data.get("settings")
    if not isinstance(settings_raw, dict):
        raise PolicyError("settings must be an object")
    settings = validate_policy_settings(settings_raw)
    body: dict[str, object] = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "policy_id": policy_id,
        "revision_id": revision_id,
        "created_at": created_at,
        "created_by": created_by,
        "display_name": display_name,
        "description": description,
        "settings": settings,
    }
    expected = content_sha256(body)
    declared = data.get("content_sha256")
    if verify_hash:
        if not isinstance(declared, str) or not declared.strip():
            raise PolicyError("content_sha256 required")
        if declared.strip().lower() != expected:
            raise PolicyError("content_sha256 mismatch")
    else:
        declared = expected
    return PolicySnapshot(
        schema_version=POLICY_SCHEMA_VERSION,
        policy_id=policy_id,
        revision_id=revision_id,
        created_at=created_at,
        created_by=created_by,
        content_sha256=str(declared).strip().lower(),
        display_name=display_name,
        description=description,
        settings=settings,
    )


def diff_snapshots(
    left: PolicySnapshot | dict[str, Any],
    right: PolicySnapshot | dict[str, Any],
) -> dict[str, object]:
    """Return a shallow diff of canonical bodies (for GitOps)."""
    left_dict = left.to_dict() if isinstance(left, PolicySnapshot) else dict(left)
    right_dict = right.to_dict() if isinstance(right, PolicySnapshot) else dict(right)
    keys = sorted(set(left_dict) | set(right_dict))
    changed: dict[str, object] = {}
    for key in keys:
        if left_dict.get(key) != right_dict.get(key):
            changed[key] = {"left": left_dict.get(key), "right": right_dict.get(key)}
    return {"changed": changed, "equal": not changed}
