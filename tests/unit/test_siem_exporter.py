"""SIEM exporter retry/backoff and delivery loop tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from backuplint.siem.config import SiemConfig
from backuplint.siem.event import (
    SiemCategory,
    SiemEvent,
    SiemEventFamily,
    SiemSeverity,
    presence_transition_event,
)
from backuplint.siem.exporter import SiemExporter
from backuplint.siem.queue import EnqueueResult, QueueLimits, SiemExportQueue
from backuplint.siem.telemetry import SiemExportTelemetry
from backuplint.siem.transport.base import TransportResult


@dataclass
class FakeClock:
    current: datetime

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current = self.current + timedelta(seconds=seconds)


class FakeTransport:
    name = "fake"

    def __init__(self, outcomes: list[TransportResult]) -> None:
        self.outcomes = list(outcomes)
        self.sent: list[str] = []

    def send(self, event: SiemEvent, *, config: SiemConfig) -> TransportResult:
        self.sent.append(event.event_id)
        if not self.outcomes:
            return TransportResult(True, False, 200, None, None)
        return self.outcomes.pop(0)


def _event(agent: str, boundary: str) -> SiemEvent:
    return presence_transition_event(
        agent_id=agent,
        event_family=SiemEventFamily.AGENT_OFFLINE,
        transition_boundary=boundary,
        source_role="controller",
    )


def test_exporter_delivers_and_acks(tmp_path) -> None:
    queue = SiemExportQueue(tmp_path / "q.sqlite3", limits=QueueLimits(max_items=20))
    telemetry = SiemExportTelemetry()
    transport = FakeTransport(
        [TransportResult(True, False, 200, None, None)],
    )
    config = SiemConfig(enabled=True, endpoint="https://siem.example/ingest")
    exporter = SiemExporter(queue, transport, config, telemetry)
    event = _event("agent-1", "2026-09-15T12:00:00+00:00")
    assert exporter.submit(event) is EnqueueResult.ENQUEUED
    summary = exporter.drain_once(wall_clock_budget_seconds=5.0)
    assert summary.delivered == 1
    assert queue.depth_by_status()["delivered"] == 1
    snap = telemetry.snapshot(queue)
    assert snap["delivered_total"] == 1


def test_exporter_retries_with_backoff(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC))
    queue = SiemExportQueue(tmp_path / "q.sqlite3", limits=QueueLimits(max_items=20))
    telemetry = SiemExportTelemetry()
    transport = FakeTransport(
        [
            TransportResult(False, True, 500, None, "server error"),
            TransportResult(True, False, 200, None, None),
        ],
    )
    config = SiemConfig(
        enabled=True,
        endpoint="https://siem.example/ingest",
        initial_backoff_seconds=2.0,
        max_backoff_seconds=30.0,
        jitter_ratio=0.0,
    )
    exporter = SiemExporter(
        queue,
        transport,
        config,
        telemetry,
        now=clock.now,
        rng=lambda: 0.0,
    )
    event = _event("agent-2", "2026-09-15T12:00:00+00:00")
    exporter.submit(event)
    first = exporter.drain_once()
    assert first.failed == 0
    assert first.deferred == 1
    row = queue.get_row(event.event_id)
    assert row is not None
    assert row.attempt_count == 1
    clock.advance(3.0)
    second = exporter.drain_once()
    assert second.delivered == 1


def test_delivery_failure_updates_telemetry_only(tmp_path) -> None:
    queue = SiemExportQueue(tmp_path / "q.sqlite3", limits=QueueLimits(max_items=20))
    telemetry = SiemExportTelemetry()
    transport = FakeTransport(
        [TransportResult(False, True, 503, None, "down")],
    )
    config = SiemConfig(enabled=True, endpoint="https://siem.example/ingest")
    exporter = SiemExporter(queue, transport, config, telemetry)
    event = _event("agent-3", "2026-09-15T12:00:00+00:00")
    exporter.submit(event)
    exporter.drain_once()
    snap = telemetry.snapshot(queue)
    assert snap["failed_total"] == 1
    assert snap["delivered_total"] == 0
    families = [
        row.event_family
        for row in [queue.get_row(event.event_id)]
        if row is not None
    ]
    assert SiemEventFamily.SIEM_DELIVERY_ERROR.value not in families


def test_submit_respects_filter_but_keeps_critical(tmp_path) -> None:
    queue = SiemExportQueue(tmp_path / "q.sqlite3", limits=QueueLimits(max_items=20))
    telemetry = SiemExportTelemetry()
    transport = FakeTransport([])
    config = SiemConfig(
        enabled=True,
        endpoint="https://siem.example/ingest",
        min_severity=SiemSeverity.HIGH,
        allowed_families=frozenset({SiemEventFamily.QUEUE_OVERFLOW}),
    )
    exporter = SiemExporter(queue, transport, config, telemetry)
    low = presence_transition_event(
        agent_id="agent-low",
        event_family=SiemEventFamily.AGENT_ONLINE,
        transition_boundary="2026-09-15T12:00:00+00:00",
        source_role="controller",
    )
    assert exporter.submit(low) is EnqueueResult.REJECTED_HEALTHY_AT_CAPACITY
    critical = SiemEvent(
        schema_version=1,
        event_id="55555555-5555-4555-8555-555555555555",
        event_family=SiemEventFamily.QUEUE_OVERFLOW,
        severity=SiemSeverity.CRITICAL,
        category=SiemCategory.FLEET_TRANSPORT,
        occurred_at="2026-09-15T12:00:00+00:00",
        received_at="2026-09-15T12:00:00+00:00",
        source_role="controller",
        summary="overflow",
        agent_id="agent-critical",
    )
    assert exporter.submit(critical) is EnqueueResult.ENQUEUED
