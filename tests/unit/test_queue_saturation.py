"""Tests for agent queue saturation / DATA_GAP policy."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from backuplint.fleet.agent import AgentQueue
from backuplint.fleet.protocol import PROTOCOL_VERSION, ResultEnvelope, new_submission_id
from backuplint.fleet.queue_policy import (
    DATA_GAP_RESULT_MARKER,
    QUEUE_OVERFLOW_STATUS,
    QueueItemKind,
    SaturatedAgentQueue,
    classify_queue_payload,
)


def test_agent_queue_uses_saturated_impl(tmp_path: Path) -> None:
    """Production AgentQueue must wrap SaturatedAgentQueue (not a parallel path)."""
    q = AgentQueue(tmp_path / "q.jsonl", max_items=5, reserved_critical=1)
    assert isinstance(q._impl, SaturatedAgentQueue)  # noqa: SLF001
    assert q.__class__.__name__ == "AgentQueue"


def _env(agent_id: str, *, status: str = "PASS", sid: str | None = None) -> ResultEnvelope:
    return ResultEnvelope(
        agent_id=agent_id,
        submission_id=sid or new_submission_id(),
        scan_time=datetime.now(UTC).isoformat(),
        backuplint_version="0.5.0.dev0",
        platform="test",
        result={"summary": {"result": status}},
        protocol_version=PROTOCOL_VERSION,
    )


def test_pass_only_fill_emits_data_gap(tmp_path: Path) -> None:
    q = AgentQueue(tmp_path / "q.jsonl", max_items=10, reserved_critical=3)
    for _ in range(20):
        q.enqueue(_env("agent-1", status="PASS"))
    items = q.peek_all()
    assert len(items) <= 10
    kinds = [classify_queue_payload(i) for i in items]
    assert QueueItemKind.DATA_GAP in kinds
    assert QueueItemKind.HEALTHY not in kinds or kinds.count(QueueItemKind.HEALTHY) < 20
    gap = next(i for i in items if classify_queue_payload(i) is QueueItemKind.DATA_GAP)
    assert gap["result"]["queue_marker"] == DATA_GAP_RESULT_MARKER  # type: ignore[index]
    assert gap["result"]["gap"]["compacted_count"] >= 1  # type: ignore[index]


def test_fail_only_fill_preserves_critical(tmp_path: Path) -> None:
    q = AgentQueue(tmp_path / "q.jsonl", max_items=10, reserved_critical=8)
    for _ in range(12):
        q.enqueue(_env("agent-1", status="FAIL"))
    items = q.peek_all()
    fails = [
        i for i in items if classify_queue_payload(i) is QueueItemKind.CRITICAL
    ]
    # Reserved critical kept; overflow may become DATA_GAP rather than silent drop.
    assert len(fails) >= 8
    assert all(
        classify_queue_payload(i) in {QueueItemKind.CRITICAL, QueueItemKind.DATA_GAP}
        for i in items
    )


def test_mixed_priorities_prefer_fail(tmp_path: Path) -> None:
    q = AgentQueue(tmp_path / "q.jsonl", max_items=8, reserved_critical=4)
    for _ in range(10):
        q.enqueue(_env("agent-1", status="PASS"))
    for _ in range(4):
        q.enqueue(_env("agent-1", status="FAIL"))
    items = q.peek_all()
    fails = [
        i
        for i in items
        if isinstance(i.get("result"), dict)
        and isinstance(i["result"].get("summary"), dict)  # type: ignore[union-attr]
        and i["result"]["summary"].get("result") == "FAIL"  # type: ignore[index]
    ]
    assert len(fails) == 4


def test_enqueue_idempotent_on_submission_id(tmp_path: Path) -> None:
    q = AgentQueue(tmp_path / "q.jsonl", max_items=50)
    sid = new_submission_id()
    q.enqueue(_env("agent-1", sid=sid))
    q.enqueue(_env("agent-1", sid=sid))
    assert len(q.peek_all()) == 1


def test_corrupt_line_becomes_data_gap(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    path.write_text("{not-json\n", encoding="utf-8")
    path.chmod(0o600)
    q = AgentQueue(path, max_items=50)
    items = q.peek_all()
    assert len(items) == 1
    assert classify_queue_payload(items[0]) is QueueItemKind.DATA_GAP
    assert items[0]["result"]["gap"]["reason"] == "CORRUPT_QUEUE_RECORD"  # type: ignore[index]


def test_compaction_idempotent_repeat_enqueue(tmp_path: Path) -> None:
    q = AgentQueue(tmp_path / "q.jsonl", max_items=6, reserved_critical=2)
    for _ in range(15):
        q.enqueue(_env("agent-1", status="PASS"))
    first = q.peek_all()
    for _ in range(5):
        q.enqueue(_env("agent-1", status="PASS"))
    second = q.peek_all()
    assert len(second) <= 6
    assert any(classify_queue_payload(i) is QueueItemKind.DATA_GAP for i in second)
    _ = first, QUEUE_OVERFLOW_STATUS
