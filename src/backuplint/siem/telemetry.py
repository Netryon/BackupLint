"""SIEM export telemetry snapshots."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backuplint.siem.queue import redact_queue_error


@dataclass(frozen=True)
class BackoffState:
    current_interval_seconds: float
    consecutive_failures: int

    def to_dict(self) -> dict[str, float | int]:
        return {
            "current_interval_seconds": self.current_interval_seconds,
            "consecutive_failures": self.consecutive_failures,
        }


class SiemExportTelemetry:
    """In-process SIEM export health counters (separate from controller telemetry)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._delivered_total = 0
        self._failed_total = 0
        self._dead_letter_total = 0
        self._retry_count_total = 0
        self._coalescing_events_total = 0
        self._overflow_warnings_total = 0
        self._last_success_at: str | None = None
        self._last_error: str | None = None
        self._last_error_at: str | None = None
        self._backoff = BackoffState(0.0, 0)
        self._endpoint_health = "healthy"

    def record_delivery_success(self) -> None:
        with self._lock:
            self._delivered_total += 1
            self._last_success_at = datetime.now(UTC).isoformat()
            self._backoff = BackoffState(0.0, 0)
            self._endpoint_health = "healthy"

    def record_delivery_failure(
        self,
        error: str | None,
        *,
        retryable: bool,
        backoff_seconds: float,
        consecutive_failures: int,
    ) -> None:
        with self._lock:
            if retryable:
                self._failed_total += 1
            self._retry_count_total += 1
            self._last_error = redact_queue_error(error)
            self._last_error_at = datetime.now(UTC).isoformat()
            self._backoff = BackoffState(backoff_seconds, consecutive_failures)
            if consecutive_failures >= 5:
                self._endpoint_health = "down"
            elif consecutive_failures >= 2:
                self._endpoint_health = "degraded"
            else:
                self._endpoint_health = "healthy"

    def record_dead_letter(self, error: str | None) -> None:
        with self._lock:
            self._dead_letter_total += 1
            self._last_error = redact_queue_error(error)
            self._last_error_at = datetime.now(UTC).isoformat()

    def record_coalescing(self, count: int) -> None:
        if count <= 0:
            return
        with self._lock:
            self._coalescing_events_total += count

    def record_overflow_warning(self) -> None:
        with self._lock:
            self._overflow_warnings_total += 1

    def snapshot(
        self,
        queue: Any | None = None,
        *,
        persist: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            depth_by_status = queue.depth_by_status() if queue is not None else {}
            depth_by_priority = queue.depth_by_priority() if queue is not None else {}
            oldest_pending = (
                queue.oldest_pending_age_seconds() if queue is not None else None
            )
            payload: dict[str, Any] = {
                "ts": time.time(),
                "queue_depth_by_status": depth_by_status,
                "queue_depth_by_priority": depth_by_priority,
                "oldest_pending_age_seconds": oldest_pending,
                "delivered_total": self._delivered_total,
                "failed_total": self._failed_total,
                "dead_letter_total": self._dead_letter_total,
                "retry_count_total": self._retry_count_total,
                "last_success_at": self._last_success_at,
                "last_error": self._last_error,
                "last_error_at": self._last_error_at,
                "backoff_state": self._backoff.to_dict(),
                "endpoint_health": self._endpoint_health,
                "coalescing_events_total": self._coalescing_events_total,
                "overflow_warnings_total": self._overflow_warnings_total,
            }
            if persist:
                self._last_snapshot = dict(payload)
            return payload

    def persist_to_jsonl(self, path: Path, snapshot: dict[str, Any] | None = None) -> None:
        payload = snapshot if snapshot is not None else self.snapshot(persist=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, separators=(",", ":")) + "\n"
        with self._lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
