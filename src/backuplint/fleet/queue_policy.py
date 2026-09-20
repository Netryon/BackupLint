"""Agent queue saturation policy: preserve important events, compact healthy ones.

Never silently drop-oldest. When capacity cannot retain exact healthy detail,
emit an explicit DATA_GAP / QUEUE_OVERFLOW marker that can be submitted and
retried like any other envelope payload.

Mutations are serialized with an exclusive flock so concurrent agent processes
cannot lose updates via last-writer-wins replace.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from backuplint import __version__
from backuplint.fleet.protocol import ResultEnvelope, new_submission_id

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]


class QueueItemKind(StrEnum):
    CRITICAL = "critical"  # FAIL/ERROR/security/state-change/data-gap
    HEALTHY = "healthy"  # repetitive PASS / heartbeat-like
    DATA_GAP = "data_gap"


DATA_GAP_RESULT_MARKER = "DATA_GAP"
QUEUE_OVERFLOW_STATUS = "QUEUE_OVERFLOW"


def classify_queue_payload(item: dict[str, object]) -> QueueItemKind:
    """Classify a queued envelope dict for retention priority."""
    result = item.get("result")
    if not isinstance(result, dict):
        return QueueItemKind.CRITICAL
    if result.get("queue_marker") in (DATA_GAP_RESULT_MARKER, QUEUE_OVERFLOW_STATUS):
        return QueueItemKind.DATA_GAP
    if result.get("data_gap") is True:
        return QueueItemKind.DATA_GAP
    summary = result.get("summary")
    status = ""
    if isinstance(summary, dict):
        status = str(summary.get("result") or summary.get("status") or "").upper()
    elif isinstance(result.get("result"), str):
        status = str(result.get("result") or "").upper()
    if status in {"FAIL", "ERROR", "WARN", "WARNING"}:
        return QueueItemKind.CRITICAL
    if result.get("security") or result.get("state_change"):
        return QueueItemKind.CRITICAL
    if status in {"PASS", "OK", "HEALTHY", ""}:
        return QueueItemKind.HEALTHY
    return QueueItemKind.CRITICAL


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _scan_time(item: dict[str, object]) -> str:
    val = item.get("scan_time")
    return str(val) if val is not None else _iso_now()


def corrupt_line_submission_id(line: str) -> str:
    """Deterministic submission_id for a corrupt queue line (stable across reloads)."""
    digest = hashlib.sha256(line.encode("utf-8", errors="replace")).hexdigest()
    return f"gap-corrupt-{digest[:32]}"


def _corrupt_gap_item(line: str, *, reason: str) -> dict[str, object]:
    now = _iso_now()
    return {
        "protocol_version": 1,
        "agent_id": "unknown",
        "submission_id": corrupt_line_submission_id(line),
        "scan_time": now,
        "backuplint_version": __version__,
        "platform": "queue",
        "result": {
            "summary": {"result": DATA_GAP_RESULT_MARKER},
            "queue_marker": DATA_GAP_RESULT_MARKER,
            "data_gap": True,
            "gap": {
                "reason": reason,
                "compacted_count": 1,
                "first_scan_time": now,
                "last_scan_time": now,
                "submission_ids": [],
            },
        },
    }


def build_data_gap_envelope(
    *,
    agent_id: str,
    compacted: list[dict[str, object]],
    reason: str = QUEUE_OVERFLOW_STATUS,
) -> dict[str, object]:
    """Build a durable DATA_GAP envelope dict from compacted healthy items."""
    times = [_scan_time(i) for i in compacted]
    first_ts = min(times) if times else _iso_now()
    last_ts = max(times) if times else _iso_now()
    identities = sorted(
        {
            str(i.get("submission_id"))
            for i in compacted
            if i.get("submission_id") is not None
        }
    )
    return {
        "protocol_version": compacted[0].get("protocol_version", 1) if compacted else 1,
        "agent_id": agent_id,
        "submission_id": new_submission_id(),
        "scan_time": last_ts,
        "backuplint_version": (
            str(compacted[0].get("backuplint_version")) if compacted else __version__
        ),
        "platform": str(compacted[0].get("platform")) if compacted else "queue",
        "run_id": new_submission_id(),
        "result": {
            "summary": {"result": DATA_GAP_RESULT_MARKER, "status": reason},
            "queue_marker": DATA_GAP_RESULT_MARKER,
            "data_gap": True,
            "gap": {
                "reason": reason,
                "compacted_count": len(compacted),
                "first_scan_time": first_ts,
                "last_scan_time": last_ts,
                "submission_ids": identities[:64],
                "submission_ids_truncated": max(0, len(identities) - 64),
            },
        },
    }


class SaturatedAgentQueue:
    """Durable JSONL queue with reserved critical capacity and healthy compaction.

    Replaces silent drop-oldest. Prefer constructing via :class:`AgentQueue` which
    delegates to this policy when ``saturation_policy=True`` (default going forward).
    """

    def __init__(
        self,
        path: Path,
        *,
        max_items: int = 200,
        reserved_critical: int = 32,
    ) -> None:
        if reserved_critical < 1:
            raise ValueError("reserved_critical must be >= 1")
        # Tiny test queues: shrink reservation so max_items=3 still works.
        if max_items <= reserved_critical:
            reserved_critical = max(1, max_items // 2)
        if max_items < reserved_critical + 1:
            raise ValueError("max_items must leave room beyond reserved_critical")
        self.path = path
        self.max_items = max_items
        self.reserved_critical = reserved_critical
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch(mode=0o600)
            self.path.chmod(0o600)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        if fcntl is None:  # pragma: no cover - non-POSIX
            yield
            return
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def enqueue(self, envelope: ResultEnvelope | dict[str, object]) -> None:
        item = envelope.to_dict() if isinstance(envelope, ResultEnvelope) else dict(envelope)
        with self._locked():
            items = self._load_unlocked()
            sid = item.get("submission_id")
            if sid is not None and any(i.get("submission_id") == sid for i in items):
                return
            items.append(item)
            items = self._enforce_capacity(items)
            self._save_unlocked(items)

    def peek_all(self) -> list[dict[str, object]]:
        with self._locked():
            return self._load_unlocked()

    def remove(self, submission_id: str) -> None:
        with self._locked():
            items = [
                i for i in self._load_unlocked() if i.get("submission_id") != submission_id
            ]
            self._save_unlocked(items)

    def _enforce_capacity(self, items: list[dict[str, object]]) -> list[dict[str, object]]:
        if len(items) <= self.max_items:
            return items
        # Compact consecutive/repetitive healthy PASS items into one DATA_GAP.
        while len(items) > self.max_items:
            healthy_idx = [
                i
                for i, it in enumerate(items)
                if classify_queue_payload(it) is QueueItemKind.HEALTHY
            ]
            if len(healthy_idx) >= 2:
                compacted = [items[i] for i in healthy_idx]
                agent_id = str(compacted[0].get("agent_id") or "unknown")
                gap = build_data_gap_envelope(agent_id=agent_id, compacted=compacted)
                items = [
                    it
                    for i, it in enumerate(items)
                    if classify_queue_payload(it) is not QueueItemKind.HEALTHY
                ]
                items.append(gap)
                continue
            gap_idx = [
                i
                for i, it in enumerate(items)
                if classify_queue_payload(it) is QueueItemKind.DATA_GAP
            ]
            if len(gap_idx) >= 2:
                first, second = gap_idx[0], gap_idx[1]
                merged_payloads = [items[first], items[second]]
                agent_id = str(merged_payloads[0].get("agent_id") or "unknown")
                total = 0
                for g in merged_payloads:
                    res = g.get("result")
                    if isinstance(res, dict):
                        gap = res.get("gap")
                        if isinstance(gap, dict):
                            total += int(gap.get("compacted_count") or 1)
                        else:
                            total += 1
                new_gap = build_data_gap_envelope(
                    agent_id=agent_id, compacted=merged_payloads
                )
                new_gap["result"]["gap"]["compacted_count"] = total  # type: ignore[index]
                items = [it for i, it in enumerate(items) if i not in {first, second}]
                items.append(new_gap)
                continue
            break
        if len(items) > self.max_items:
            critical = [
                it for it in items if classify_queue_payload(it) is QueueItemKind.CRITICAL
            ]
            gaps = [
                it for it in items if classify_queue_payload(it) is QueueItemKind.DATA_GAP
            ]
            healthy = [
                it for it in items if classify_queue_payload(it) is QueueItemKind.HEALTHY
            ]
            keep_critical = critical[-self.reserved_critical :]
            overflow = critical[: max(0, len(critical) - self.reserved_critical)] + healthy
            if overflow:
                agent_id = str(overflow[0].get("agent_id") or "unknown")
                gap = build_data_gap_envelope(
                    agent_id=agent_id,
                    compacted=overflow,
                    reason=QUEUE_OVERFLOW_STATUS,
                )
                items = keep_critical + gaps + [gap]
            items = items[-self.max_items :]
        return items

    def _load_unlocked(self) -> list[dict[str, object]]:
        text = self.path.read_text(encoding="utf-8")
        if not text.strip():
            return []
        items: list[dict[str, object]] = []
        rewritten = False
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                items.append(_corrupt_gap_item(line, reason="CORRUPT_QUEUE_RECORD"))
                rewritten = True
                continue
            if isinstance(data, dict):
                items.append(data)
            else:
                items.append(
                    _corrupt_gap_item(line, reason="NON_OBJECT_QUEUE_RECORD")
                )
                rewritten = True
        if rewritten:
            # Persist deterministic gap markers so remove()/reload stay idempotent.
            self._save_unlocked(items)
        return items

    def _save_unlocked(self, items: list[dict[str, object]]) -> None:
        tmp = self.path.with_suffix(".tmp")
        payload = "\n".join(json.dumps(i, separators=(",", ":")) for i in items)
        if payload:
            payload += "\n"
        tmp.write_text(payload, encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, self.path)
