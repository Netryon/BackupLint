"""Background SIEM export worker: dequeue, transport send, retry/backoff."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from backuplint.siem.config import SiemConfig
from backuplint.siem.event import SIEM_EVENT_SCHEMA_VERSION
from backuplint.siem.queue import EnqueueResult, SiemExportQueue
from backuplint.siem.telemetry import SiemExportTelemetry
from backuplint.siem.transport.base import Transport, TransportResult


@dataclass(frozen=True)
class DrainSummary:
    attempted: int
    delivered: int
    failed: int
    deferred: int


class SiemExporter:
    """Bounded exporter loop over the durable SIEM queue."""

    def __init__(
        self,
        queue: SiemExportQueue,
        transport: Transport,
        config: SiemConfig,
        telemetry: SiemExportTelemetry,
        *,
        rng: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.queue = queue
        self.transport = transport
        self.config = config
        self.telemetry = telemetry
        self._rng = rng or random.random
        self._sleep = sleeper or time.sleep
        self._now = now or (lambda: datetime.now(UTC))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._consecutive_failures = 0

    def submit(self, event, *, now: str | None = None) -> EnqueueResult:
        if not self.config.enabled:
            return EnqueueResult.DUPLICATE
        if not self.config.should_export(event.event_family, event.severity):
            return EnqueueResult.REJECTED_HEALTHY_AT_CAPACITY
        result = self.queue.enqueue(event, now=now or self._now().isoformat())
        if result is EnqueueResult.OVERFLOW_HARD_CEILING:
            self.telemetry.record_overflow_warning()
        elif self.queue.at_soft_cap():
            self.telemetry.record_overflow_warning()
        return result

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="siem-exporter",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def drain_once(
        self,
        *,
        wall_clock_budget_seconds: float = 10.0,
        max_items: int | None = None,
    ) -> DrainSummary:
        deadline = time.monotonic() + wall_clock_budget_seconds
        attempted = delivered = failed = deferred = 0
        limit = max_items if max_items is not None else 64
        while attempted < limit and time.monotonic() < deadline:
            if self._stop.is_set():
                break
            outcome = self._process_one()
            if outcome is None:
                break
            attempted += 1
            if outcome == "delivered":
                delivered += 1
            elif outcome == "failed":
                failed += 1
            else:
                deferred += 1
            if outcome == "deferred":
                break
        self._maintenance_pass()
        return DrainSummary(
            attempted=attempted,
            delivered=delivered,
            failed=failed,
            deferred=deferred,
        )

    def _run_loop(self) -> None:
        idle_sleep = 0.25
        while not self._stop.is_set():
            outcome = self._process_one()
            if outcome is None:
                self._maintenance_pass()
                self._stop.wait(idle_sleep)
                continue
            if outcome == "deferred":
                delay = self._current_backoff_seconds()
                self._stop.wait(min(delay, self.config.max_backoff_seconds))
            else:
                idle_sleep = 0.25

    def _process_one(self) -> str | None:
        if not self.config.enabled:
            return None
        now_iso = self._now().isoformat()
        row = self.queue.claim_next(now=now_iso)
        if row is None:
            return None

        if row.schema_version > SIEM_EVENT_SCHEMA_VERSION:
            self.queue.mark_dead_letter(
                row.event_id,
                last_error="unsupported schema_version",
            )
            self.telemetry.record_dead_letter("unsupported schema_version")
            return "failed"

        try:
            event = row.parse_event()
        except Exception as exc:
            self.queue.mark_dead_letter(row.event_id, last_error=str(exc))
            self.telemetry.record_dead_letter(str(exc))
            return "failed"

        result = self.transport.send(event, config=self.config)
        return self._handle_result(row.event_id, row.attempt_count, result)

    def _handle_result(
        self,
        event_id: str,
        attempt_count: int,
        result: TransportResult,
    ) -> str:
        if result.delivered:
            self.queue.mark_delivered(event_id)
            self.telemetry.record_delivery_success()
            self._consecutive_failures = 0
            return "delivered"

        attempt = attempt_count + 1
        self._consecutive_failures += 1
        backoff = self._compute_backoff(attempt, result.retry_after_seconds)
        error = result.error

        if (
            self.config.max_attempts is not None
            and attempt >= self.config.max_attempts
            and not result.retryable
        ):
            self.queue.mark_dead_letter(event_id, last_error=error)
            self.telemetry.record_dead_letter(error)
            return "failed"

        if self.config.max_attempts is not None and attempt >= self.config.max_attempts:
            self.queue.mark_dead_letter(event_id, last_error=error)
            self.telemetry.record_dead_letter(error)
            return "failed"

        next_at = (self._now() + timedelta(seconds=backoff)).isoformat()
        self.queue.mark_retry(
            event_id,
            attempt_count=attempt,
            next_attempt_at=next_at,
            last_error=error,
        )
        self.telemetry.record_delivery_failure(
            error,
            retryable=result.retryable,
            backoff_seconds=backoff,
            consecutive_failures=self._consecutive_failures,
        )
        return "deferred" if result.retryable else "failed"

    def _compute_backoff(
        self,
        attempt: int,
        retry_after: float | None,
    ) -> float:
        if retry_after is not None:
            return min(float(retry_after), self.config.max_backoff_seconds)
        base = self.config.initial_backoff_seconds
        cap = self.config.max_backoff_seconds
        delay = min(cap, base * (2 ** max(0, attempt - 1)))
        jitter = self.config.jitter_ratio
        factor = 1.0 - jitter + (jitter * self._rng())
        return min(cap, delay * factor)

    def _current_backoff_seconds(self) -> float:
        return self._compute_backoff(self._consecutive_failures + 1, None)

    def _maintenance_pass(self) -> None:
        coalesced = self.queue.coalesce_healthy_rows()
        if coalesced:
            self.telemetry.record_coalescing(coalesced)
        if self.queue.at_soft_cap():
            self.telemetry.record_overflow_warning()
        self.queue.purge_delivered()
        self.queue.purge_dead_letter()
        stale = (self._now() - timedelta(minutes=30)).isoformat()
        self.queue.requeue_in_flight(stale_before=stale)
