"""Security-focused policy tests (v0.8 campaign subset)."""

from __future__ import annotations

from pathlib import Path

import pytest

from backuplint.fleet.controller_store import ControllerStore, ControllerStoreError
from backuplint.policy.schema import PolicyError, build_policy_snapshot


def test_raw_password_in_settings_rejected() -> None:
    with pytest.raises(PolicyError):
        build_policy_snapshot(
            policy_id="pol-sec01",
            created_by="t",
            display_name="x",
            settings={"schedule": {"password": "hunter2"}},
        )


def test_oversized_policy_rejected() -> None:
    from backuplint.policy.schema import MAX_POLICY_BYTES, content_sha256

    huge_body = {
        "schema_version": 1,
        "policy_id": "pol-sec02",
        "revision_id": "rev-x",
        "created_at": "2026-01-01T00:00:00+00:00",
        "created_by": "t",
        "display_name": "x",
        "description": "x" * (MAX_POLICY_BYTES + 1),
        "settings": {},
    }
    with pytest.raises(PolicyError):
        content_sha256(huge_body)


def test_revoked_agent_cannot_fetch_policy(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "c.sqlite3")
    store.register_agent(agent_id="agent-revoked1", label="r", hostname="h")
    snap = store.policy_create(
        display_name="p",
        description="",
        settings={"reporting": {"policy_poll_interval": "5m"}},
        created_by="op",
    )
    store.policy_assign_agent("agent-revoked1", snap.revision_id)
    store.set_agent_status("agent-revoked1", "revoked")
    with pytest.raises(ControllerStoreError, match="revoked"):
        store.policy_get_desired("agent-revoked1")


def test_audit_records_no_secret_values(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "c.sqlite3")
    store.register_agent(agent_id="agent-audit1", label="a", hostname="h")
    store.policy_create(
        display_name="p",
        description="",
        settings={
            "siem": {
                "enabled": True,
                "auth_token": {"source": "env", "name": "SIEM_TOKEN"},
            }
        },
        created_by="op",
    )
    audit = store.policy_list_audit(limit=5)
    blob = str(audit).lower()
    assert "hunter2" not in blob
    assert "password" not in blob
    assert any(row["action"] == "policy.create" for row in audit)
