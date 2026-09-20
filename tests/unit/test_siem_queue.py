"""SIEM export queue durability and overflow tests."""

from __future__ import annotations

from backuplint.siem.event import (
    SiemCategory,
    SiemEvent,
    SiemEventFamily,
    SiemSeverity,
    presence_transition_event,
)
from backuplint.siem.queue import (
    EnqueueResult,
    QueueLimits,
    SiemExportQueue,
)


def _online_event(agent_id: str, boundary: str) -> SiemEvent:
    return presence_transition_event(
        agent_id=agent_id,
        event_family=SiemEventFamily.AGENT_ONLINE,
        transition_boundary=boundary,
        source_role="controller",
    )


def _critical_event(event_id: str) -> SiemEvent:
    return SiemEvent(
        schema_version=1,
        event_id=event_id,
        event_family=SiemEventFamily.QUEUE_OVERFLOW,
        severity=SiemSeverity.CRITICAL,
        category=SiemCategory.FLEET_TRANSPORT,
        occurred_at="2026-09-15T12:00:00+00:00",
        received_at="2026-09-15T12:00:00+00:00",
        source_role="controller",
        summary="Queue overflow",
        agent_id="agent-critical",
    )


def test_enqueue_dequeue_durability_across_reopen(tmp_path) -> None:
    db_path = tmp_path / "siem" / "siem_export.sqlite3"
    event = _online_event("agent-a", "2026-09-15T12:00:00+00:00")
    queue = SiemExportQueue(db_path, limits=QueueLimits(max_items=100))
    assert queue.enqueue(event) is EnqueueResult.ENQUEUED
    queue.close()

    queue2 = SiemExportQueue(db_path, limits=QueueLimits(max_items=100))
    try:
        assert queue2.pending_count() == 1
        row = queue2.claim_next()
        assert row is not None
        assert row.event_id == event.event_id
        queue2.mark_delivered(event.event_id)
        assert queue2.depth_by_status()["delivered"] == 1
    finally:
        queue2.close()


def test_duplicate_event_id_is_noop(tmp_path) -> None:
    db_path = tmp_path / "siem_export.sqlite3"
    event = _online_event("agent-a", "2026-09-15T12:00:00+00:00")
    queue = SiemExportQueue(db_path, limits=QueueLimits(max_items=50))
    try:
        assert queue.enqueue(event) is EnqueueResult.ENQUEUED
        assert queue.enqueue(event) is EnqueueResult.DUPLICATE
        assert queue.pending_count() == 1
    finally:
        queue.close()


def test_coalescing_merges_healthy_rows_at_soft_cap(tmp_path) -> None:
    limits = QueueLimits(max_items=10, soft_cap_ratio=0.5, reserved_critical=4)
    db_path = tmp_path / "siem_export.sqlite3"
    queue = SiemExportQueue(db_path, limits=limits)
    try:
        for index in range(6):
            queue.enqueue(
                _online_event(f"agent-{index}", f"2026-09-15T12:0{index}:00+00:00")
            )
        coalesced = queue.coalesce_healthy_rows()
        assert coalesced >= 2
        assert queue.pending_count() < 6
        critical = _critical_event("11111111-1111-4111-8111-111111111111")
        assert queue.enqueue(critical) is EnqueueResult.ENQUEUED
        row = queue.get_row(critical.event_id)
        assert row is not None
        assert row.priority_class == "critical"
    finally:
        queue.close()


def test_hard_ceiling_blocks_enqueue_but_keeps_critical_visible(tmp_path) -> None:
    limits = QueueLimits(max_items=4, soft_cap_ratio=0.5, hard_ceiling_multiplier=1.5)
    db_path = tmp_path / "siem_export.sqlite3"
    queue = SiemExportQueue(db_path, limits=limits)
    try:
        for index in range(6):
            queue.enqueue(
                _critical_event(f"22222222-2222-4222-8222-2222222222{index:02d}")
            )
        result = queue.enqueue(
            _critical_event("33333333-3333-4333-8333-333333333333")
        )
        assert result is EnqueueResult.OVERFLOW_HARD_CEILING
        assert queue.pending_count() >= limits.max_items
    finally:
        queue.close()
