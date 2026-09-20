"""Agent local policy apply engine tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from backuplint.fleet.policy_apply import (
    ManagedPolicyDir,
    PolicyApplyError,
    apply_policy_response,
    rollback_to_previous,
)
from backuplint.policy.schema import build_policy_snapshot


def _settings() -> dict[str, object]:
    return {"reporting": {"policy_poll_interval": "5m"}}


def _response(snap: object, generation: int) -> dict[str, object]:
    return {
        "status": "ASSIGNED",
        "assignment_generation": generation,
        "revision_id": snap.revision_id,
        "content_sha256": snap.content_sha256,
        "policy": snap.to_dict(),
    }


def test_apply_atomic_and_known_good(tmp_path: Path) -> None:
    managed = ManagedPolicyDir(tmp_path / "managed-policy")
    snap = build_policy_snapshot(
        policy_id="pol-apply01",
        created_by="t",
        display_name="A",
        settings=_settings(),
    )
    applied = apply_policy_response(
        managed, _response(snap, 1), applied_at="2026-01-01T00:00:00+00:00"
    )
    assert applied.apply_status == "success"
    assert managed.current_path.exists()
    assert managed.current_path.stat().st_mode & 0o777 == 0o600

    snap2 = build_policy_snapshot(
        policy_id="pol-apply01",
        created_by="t",
        display_name="B",
        settings={**_settings(), "queue": {"max_items": 50}},
    )
    apply_policy_response(
        managed, _response(snap2, 2), applied_at="2026-01-01T00:01:00+00:00"
    )
    assert managed.previous_path.exists()


def test_reject_stale_generation(tmp_path: Path) -> None:
    managed = ManagedPolicyDir(tmp_path / "managed-policy")
    snap = build_policy_snapshot(
        policy_id="pol-apply02",
        created_by="t",
        display_name="A",
        settings=_settings(),
    )
    apply_policy_response(
        managed, _response(snap, 5), applied_at="2026-01-01T00:00:00+00:00"
    )
    with pytest.raises(PolicyApplyError, match="stale"):
        apply_policy_response(
            managed, _response(snap, 3), applied_at="2026-01-01T00:01:00+00:00"
        )


def test_rollback_to_previous(tmp_path: Path) -> None:
    managed = ManagedPolicyDir(tmp_path / "managed-policy")
    snap1 = build_policy_snapshot(
        policy_id="pol-apply03",
        created_by="t",
        display_name="v1",
        settings=_settings(),
    )
    apply_policy_response(
        managed, _response(snap1, 1), applied_at="2026-01-01T00:00:00+00:00"
    )
    snap2 = build_policy_snapshot(
        policy_id="pol-apply03",
        created_by="t",
        display_name="v2",
        settings={**_settings(), "queue": {"max_items": 10}},
    )
    apply_policy_response(
        managed, _response(snap2, 2), applied_at="2026-01-01T00:01:00+00:00"
    )
    rolled = rollback_to_previous(
        managed, new_generation=3, applied_at="2026-01-01T00:02:00+00:00"
    )
    assert rolled.revision_id == snap1.revision_id


def test_reject_symlink(tmp_path: Path) -> None:
    managed = ManagedPolicyDir(tmp_path / "managed-policy")
    managed.current_path.symlink_to("/etc/passwd")
    with pytest.raises(PolicyApplyError, match="symlink"):
        managed.load_state()
