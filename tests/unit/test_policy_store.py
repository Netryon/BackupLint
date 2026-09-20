"""Controller policy store CRUD and assignment tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from backuplint.fleet.controller_store import (
    STORE_SCHEMA_VERSION,
    ControllerStore,
    ControllerStoreError,
)


def _settings() -> dict[str, object]:
    return {
        "schedule": {"coverage": {"enabled": True, "every": "30m"}},
        "reporting": {"policy_poll_interval": "5m"},
    }


@pytest.fixture
def store(tmp_path: Path) -> ControllerStore:
    s = ControllerStore(tmp_path / "c.sqlite3")
    s.register_agent(agent_id="agent-policy01", label="a", hostname="h1")
    s.register_agent(agent_id="agent-policy02", label="b", hostname="h2")
    return s


def test_schema_v7_migration(store: ControllerStore) -> None:
    assert store.schema_version() == STORE_SCHEMA_VERSION == 7


def test_create_assign_and_resolve(store: ControllerStore) -> None:
    snap = store.policy_create(
        display_name="Base",
        description="",
        settings=_settings(),
        created_by="op",
    )
    gen1 = store.policy_assign_agent("agent-policy01", snap.revision_id)
    desired = store.policy_get_desired("agent-policy01")
    assert desired["status"] == "ASSIGNED"
    assert desired["assignment_generation"] == gen1
    no_change = store.policy_get_desired(
        "agent-policy01",
        current_assignment_generation=gen1,
        current_revision_id=snap.revision_id,
        current_content_sha256=snap.content_sha256,
    )
    assert no_change["status"] == "NO_CHANGE"


def test_assignment_generation_monotonic_on_rollback(store: ControllerStore) -> None:
    snap1 = store.policy_create(
        display_name="v1", description="", settings=_settings(), created_by="op"
    )
    gen1 = store.policy_assign_agent("agent-policy01", snap1.revision_id)
    snap2 = store.policy_create_revision(
        snap1.policy_id,
        display_name="v2",
        description="",
        settings={
            **(_settings()),
            "queue": {"max_items": 100},
        },
        created_by="op",
    )
    gen2 = store.policy_assign_agent("agent-policy01", snap2.revision_id)
    assert gen2 > gen1
    gen3 = store.policy_rollback_agent("agent-policy01", snap1.revision_id)
    assert gen3 > gen2


def test_group_conflict_rejected(store: ControllerStore) -> None:
    snap_a = store.policy_create(
        display_name="A", description="", settings=_settings(), created_by="op"
    )
    snap_b = store.policy_create(
        display_name="B", description="", settings=_settings(), created_by="op"
    )
    g1 = store.policy_group_create(display_name="g1")
    g2 = store.policy_group_create(display_name="g2")
    store.policy_group_add_member(g1, "agent-policy01")
    store.policy_group_add_member(g2, "agent-policy01")
    store.policy_assign_group(g1, snap_a.revision_id)
    with pytest.raises(ControllerStoreError, match="conflicting"):
        store.policy_assign_group(g2, snap_b.revision_id)


def test_applied_state_and_audit(store: ControllerStore) -> None:
    snap = store.policy_create(
        display_name="A", description="", settings=_settings(), created_by="op"
    )
    gen = store.policy_assign_agent("agent-policy01", snap.revision_id)
    store.policy_report_applied(
        "agent-policy01",
        assignment_generation=gen,
        revision_id=snap.revision_id,
        content_sha256=snap.content_sha256,
        apply_status="success",
        drift_status="IN_SYNC",
    )
    effective = store.policy_get_effective("agent-policy01")
    assert effective is not None
    assert effective["applied"]["apply_status"] == "success"
    audit = store.policy_list_audit(limit=10)
    assert any(row["action"] == "policy.apply.report" for row in audit)
