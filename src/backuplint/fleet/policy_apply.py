"""Agent local policy apply engine with known-good preservation."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from backuplint.policy.schema import (
    PolicySnapshot,
    canonical_json,
    content_sha256,
    parse_policy_snapshot,
)
from backuplint.policy.settings import validate_policy_settings
from backuplint.secrets import resolve_secret
from backuplint.secrets.ref import parse_secret_ref


class ApplyStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    NO_CHANGE = "no_change"
    REJECTED = "rejected"


class DriftStatus(StrEnum):
    IN_SYNC = "IN_SYNC"
    PENDING = "PENDING"
    APPLY_FAILED = "APPLY_FAILED"
    DRIFTED = "DRIFTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNAVAILABLE = "UNAVAILABLE"


class PolicyApplyError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class AppliedState:
    assignment_generation: int
    revision_id: str
    content_sha256: str
    apply_status: str
    drift_status: str
    reason: str
    applied_at: str
    local_config_sha256: str


@dataclass(frozen=True, slots=True)
class LocalPolicyState:
    current: dict[str, object] | None
    previous: dict[str, object] | None
    applied: AppliedState | None


class ManagedPolicyDir:
    """Atomic managed-policy state under agent state dir."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.current_path = self.root / "current.json"
        self.previous_path = self.root / "previous.json"
        self.applied_path = self.root / "applied.json"

    def load_state(self) -> LocalPolicyState:
        current = self._read_json(self.current_path)
        previous = self._read_json(self.previous_path)
        applied_raw = self._read_json(self.applied_path)
        applied = None
        if applied_raw is not None:
            applied = AppliedState(
                assignment_generation=int(applied_raw.get("assignment_generation", 0)),
                revision_id=str(applied_raw.get("revision_id") or ""),
                content_sha256=str(applied_raw.get("content_sha256") or ""),
                apply_status=str(applied_raw.get("apply_status") or ""),
                drift_status=str(applied_raw.get("drift_status") or ""),
                reason=str(applied_raw.get("reason") or ""),
                applied_at=str(applied_raw.get("applied_at") or ""),
                local_config_sha256=str(applied_raw.get("local_config_sha256") or ""),
            )
        return LocalPolicyState(current=current, previous=previous, applied=applied)

    @staticmethod
    def _read_json(path: Path) -> dict[str, object] | None:
        if not path.exists():
            return None
        if path.is_symlink():
            raise PolicyApplyError(f"refusing symlink policy file: {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PolicyApplyError(f"corrupt policy state: {path}") from exc
        if not isinstance(raw, dict):
            raise PolicyApplyError(f"invalid policy state: {path}")
        return raw

    def _atomic_write(self, path: Path, payload: dict[str, object]) -> None:
        if path.exists() and path.is_symlink():
            raise PolicyApplyError(f"refusing symlink target: {path}")
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.root), prefix=f".{path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
            path.chmod(0o600)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def write_current(self, snapshot: PolicySnapshot, generation: int) -> str:
        payload = {
            "assignment_generation": generation,
            "policy": snapshot.to_dict(),
        }
        local_hash = hashlib.sha256(
            canonical_json(payload).encode("utf-8")
        ).hexdigest()
        self._atomic_write(self.current_path, payload)
        return local_hash

    def rotate_known_good(self) -> None:
        if self.current_path.exists() and not self.current_path.is_symlink():
            self._atomic_write(
                self.previous_path,
                self._read_json(self.current_path) or {},
            )

    def write_applied(self, applied: AppliedState) -> None:
        self._atomic_write(
            self.applied_path,
            {
                "assignment_generation": applied.assignment_generation,
                "revision_id": applied.revision_id,
                "content_sha256": applied.content_sha256,
                "apply_status": applied.apply_status,
                "drift_status": applied.drift_status,
                "reason": applied.reason,
                "applied_at": applied.applied_at,
                "local_config_sha256": applied.local_config_sha256,
            },
        )


def _validate_capabilities(
    settings: dict[str, object], capabilities: dict[str, object] | None
) -> str | None:
    if capabilities is None:
        return None
    caps = capabilities.get("capabilities")
    if not isinstance(caps, dict):
        return None
    schedule = settings.get("schedule")
    if isinstance(schedule, dict):
        for check, job in schedule.items():
            if not isinstance(job, dict) or not job.get("enabled", True):
                continue
            cap_key = {
                "coverage": "coverage_scanning",
                "integrity": "standard_integrity",
                "deep_integrity": "deep_integrity",
                "restore_verification": "restore_verification",
            }.get(str(check))
            if cap_key is None:
                continue
            cap = caps.get(cap_key)
            if isinstance(cap, dict) and not cap.get("available", False):
                return f"capability {cap_key} unavailable"
    siem = settings.get("siem")
    if isinstance(siem, dict) and siem.get("enabled"):
        cap = caps.get("fleet_reporting")
        if isinstance(cap, dict) and not cap.get("available", False):
            return "capability fleet_reporting unavailable for SIEM"
    return None


def _dry_run_secret_refs(settings: dict[str, object]) -> str | None:
    siem = settings.get("siem")
    if not isinstance(siem, dict):
        return None
    token = siem.get("auth_token")
    if token is None:
        return None
    try:
        ref = parse_secret_ref(token, field_name="siem.auth_token")
    except Exception as exc:  # noqa: BLE001
        return f"invalid secret ref: {exc}"
    try:
        resolve_secret(ref)
    except Exception:
        return "unresolved siem.auth_token SecretRef"
    return None


def apply_policy_response(
    managed_dir: ManagedPolicyDir,
    response: dict[str, object],
    *,
    capabilities: dict[str, object] | None = None,
    applied_at: str,
) -> AppliedState:
    """Apply controller desired-policy response locally."""
    status = str(response.get("status") or "")
    state = managed_dir.load_state()
    if status == "NO_CHANGE":
        if state.applied is None:
            raise PolicyApplyError("no applied state for NO_CHANGE")
        return state.applied
    if status == "NO_ASSIGNMENT":
        raise PolicyApplyError("no policy assignment from controller")
    if status != "ASSIGNED":
        raise PolicyApplyError(f"unexpected policy status: {status}")

    generation = int(response.get("assignment_generation", 0))
    if state.applied is not None and generation < state.applied.assignment_generation:
        raise PolicyApplyError("stale assignment_generation rejected")

    policy_raw = response.get("policy")
    if not isinstance(policy_raw, dict):
        raise PolicyApplyError("missing policy snapshot")
    snapshot = parse_policy_snapshot(policy_raw)
    declared_sha = str(response.get("content_sha256") or "")
    if declared_sha != snapshot.content_sha256:
        raise PolicyApplyError("content_sha256 mismatch in response")

    body = snapshot.canonical_body()
    if content_sha256(body) != snapshot.content_sha256:
        raise PolicyApplyError("policy hash verification failed")

    cap_reason = _validate_capabilities(snapshot.settings, capabilities)
    if cap_reason:
        applied = AppliedState(
            assignment_generation=generation,
            revision_id=snapshot.revision_id,
            content_sha256=snapshot.content_sha256,
            apply_status=ApplyStatus.REJECTED.value,
            drift_status=DriftStatus.UNSUPPORTED.value,
            reason=cap_reason,
            applied_at=applied_at,
            local_config_sha256=state.applied.local_config_sha256 if state.applied else "",
        )
        managed_dir.write_applied(applied)
        return applied

    secret_reason = _dry_run_secret_refs(snapshot.settings)
    if secret_reason:
        applied = AppliedState(
            assignment_generation=generation,
            revision_id=snapshot.revision_id,
            content_sha256=snapshot.content_sha256,
            apply_status=ApplyStatus.FAILED.value,
            drift_status=DriftStatus.APPLY_FAILED.value,
            reason=secret_reason,
            applied_at=applied_at,
            local_config_sha256=state.applied.local_config_sha256 if state.applied else "",
        )
        managed_dir.write_applied(applied)
        return applied

    # Dry-run validation only — no shell/OS mutations.
    validate_policy_settings(snapshot.settings)

    if (
        state.applied is not None
        and state.applied.assignment_generation == generation
        and state.applied.revision_id == snapshot.revision_id
        and state.applied.content_sha256 == snapshot.content_sha256
        and state.applied.apply_status == ApplyStatus.SUCCESS.value
    ):
        return state.applied

    try:
        if state.current is not None:
            managed_dir.rotate_known_good()
        local_hash = managed_dir.write_current(snapshot, generation)
        applied = AppliedState(
            assignment_generation=generation,
            revision_id=snapshot.revision_id,
            content_sha256=snapshot.content_sha256,
            apply_status=ApplyStatus.SUCCESS.value,
            drift_status=DriftStatus.IN_SYNC.value,
            reason="",
            applied_at=applied_at,
            local_config_sha256=local_hash,
        )
        managed_dir.write_applied(applied)
        return applied
    except Exception as exc:  # noqa: BLE001
        applied = AppliedState(
            assignment_generation=generation,
            revision_id=snapshot.revision_id,
            content_sha256=snapshot.content_sha256,
            apply_status=ApplyStatus.FAILED.value,
            drift_status=DriftStatus.APPLY_FAILED.value,
            reason=str(exc),
            applied_at=applied_at,
            local_config_sha256=state.applied.local_config_sha256 if state.applied else "",
        )
        managed_dir.write_applied(applied)
        return applied


def rollback_to_previous(
    managed_dir: ManagedPolicyDir,
    *,
    new_generation: int,
    applied_at: str,
) -> AppliedState:
    """Automatic local rollback to previous known-good snapshot."""
    state = managed_dir.load_state()
    if state.previous is None:
        raise PolicyApplyError("no previous known-good policy")
    policy_raw = state.previous.get("policy")
    if not isinstance(policy_raw, dict):
        raise PolicyApplyError("corrupt previous policy")
    snapshot = parse_policy_snapshot(policy_raw)
    managed_dir.rotate_known_good()
    local_hash = managed_dir.write_current(snapshot, new_generation)
    applied = AppliedState(
        assignment_generation=new_generation,
        revision_id=snapshot.revision_id,
        content_sha256=snapshot.content_sha256,
        apply_status=ApplyStatus.SUCCESS.value,
        drift_status=DriftStatus.IN_SYNC.value,
        reason="local rollback",
        applied_at=applied_at,
        local_config_sha256=local_hash,
    )
    managed_dir.write_applied(applied)
    return applied
