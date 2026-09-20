"""Controller saturation telemetry: latency summaries, lock timing, peaks."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _percentile(sorted_samples: list[float], pct: float) -> float | None:
    if not sorted_samples:
        return None
    if len(sorted_samples) == 1:
        return sorted_samples[0]
    rank = (pct / 100.0) * (len(sorted_samples) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_samples) - 1)
    frac = rank - lo
    return sorted_samples[lo] * (1.0 - frac) + sorted_samples[hi] * frac


@dataclass(frozen=True)
class LatencySummary:
    count: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    max_ms: float | None
    mean_ms: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
            "max_ms": self.max_ms,
            "mean_ms": self.mean_ms,
        }


class _LatencyWindow:
    """Bounded recent samples for percentile summaries (not just counters)."""

    def __init__(self, maxlen: int = 2048) -> None:
        self._samples: deque[float] = deque(maxlen=maxlen)
        self._total_count = 0
        self._sum_ms = 0.0
        self._max_ms = 0.0
        self._lock = threading.Lock()

    def observe(self, ms: float) -> None:
        with self._lock:
            self._samples.append(ms)
            self._total_count += 1
            self._sum_ms += ms
            if ms > self._max_ms:
                self._max_ms = ms

    def summary(self) -> LatencySummary:
        with self._lock:
            samples = sorted(self._samples)
            count = self._total_count
            if not samples:
                return LatencySummary(0, None, None, None, None, None)
            mean = self._sum_ms / count if count else None
            return LatencySummary(
                count=count,
                p50_ms=_percentile(samples, 50),
                p95_ms=_percentile(samples, 95),
                p99_ms=_percentile(samples, 99),
                max_ms=self._max_ms,
                mean_ms=mean,
            )


class InstrumentedRLock:
    """RLock that records outer acquire wait and hold times into telemetry."""

    def __init__(self, telemetry: ControllerTelemetry) -> None:
        self._lock = threading.RLock()
        self._telemetry = telemetry
        self._depth = 0
        self._hold_start = 0.0
        self._owner = threading.local()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        t0 = time.perf_counter()
        if timeout is None or timeout < 0:
            ok = self._lock.acquire(blocking)
        else:
            ok = self._lock.acquire(blocking, timeout)
        waited_ms = (time.perf_counter() - t0) * 1000.0
        if ok:
            depth = getattr(self._owner, "depth", 0)
            if depth == 0:
                self._telemetry.record_db_lock_wait(waited_ms)
                self._hold_start = time.perf_counter()
            self._owner.depth = depth + 1
        return ok

    def release(self) -> None:
        depth = getattr(self._owner, "depth", 0)
        if depth <= 0:
            self._lock.release()
            return
        depth -= 1
        self._owner.depth = depth
        if depth == 0:
            held_ms = (time.perf_counter() - self._hold_start) * 1000.0
            self._telemetry.record_db_lock_hold(held_ms)
        self._lock.release()

    def __enter__(self) -> InstrumentedRLock:
        self.acquire()
        return self

    def __exit__(self, *args: object) -> None:
        self.release()


class ControllerTelemetry:
    """In-process saturation / admission telemetry for the fleet controller."""

    def __init__(self, *, sample_window: int = 2048, snapshot_history: int = 120) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self._active_peak = 0
        self._in_flight: dict[str, int] = {}
        self._endpoint_latency: dict[str, _LatencyWindow] = {}
        self._db_lock_wait = _LatencyWindow(sample_window)
        self._db_lock_hold = _LatencyWindow(sample_window)
        self._db_op_latency: dict[str, _LatencyWindow] = {}
        self._admitted = 0
        self._rejected_503 = 0
        self._http_errors = 0
        self._thread_peak = 0
        self._fd_peak = 0
        self._snapshots: deque[dict[str, Any]] = deque(maxlen=snapshot_history)
        self._sample_window = sample_window

    def _endpoint_window(self, endpoint: str) -> _LatencyWindow:
        win = self._endpoint_latency.get(endpoint)
        if win is None:
            win = _LatencyWindow(self._sample_window)
            self._endpoint_latency[endpoint] = win
        return win

    def _op_window(self, op: str) -> _LatencyWindow:
        win = self._db_op_latency.get(op)
        if win is None:
            win = _LatencyWindow(self._sample_window)
            self._db_op_latency[op] = win
        return win

    def begin_request(self, endpoint: str) -> float:
        with self._lock:
            self._active += 1
            if self._active > self._active_peak:
                self._active_peak = self._active
            self._in_flight[endpoint] = self._in_flight.get(endpoint, 0) + 1
            self._refresh_peaks_unlocked()
        return time.perf_counter()

    def end_request(self, endpoint: str, started: float, *, http_error: bool = False) -> None:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        with self._lock:
            self._active = max(0, self._active - 1)
            cur = self._in_flight.get(endpoint, 0)
            if cur <= 1:
                self._in_flight.pop(endpoint, None)
            else:
                self._in_flight[endpoint] = cur - 1
            if http_error:
                self._http_errors += 1
            self._endpoint_window(endpoint).observe(elapsed_ms)
            self._refresh_peaks_unlocked()

    def record_admission(self, *, admitted: bool) -> None:
        with self._lock:
            if admitted:
                self._admitted += 1
            else:
                self._rejected_503 += 1

    def record_db_lock_wait(self, ms: float) -> None:
        self._db_lock_wait.observe(ms)

    def record_db_lock_hold(self, ms: float) -> None:
        self._db_lock_hold.observe(ms)

    def record_db_op(self, op: str, ms: float) -> None:
        with self._lock:
            self._op_window(op).observe(ms)

    def _refresh_peaks_unlocked(self) -> None:
        threads = threading.active_count()
        if threads > self._thread_peak:
            self._thread_peak = threads
        fds = _fd_count()
        if fds is not None and fds > self._fd_peak:
            self._fd_peak = fds

    def snapshot(self, *, persist: bool = True) -> dict[str, Any]:
        with self._lock:
            self._refresh_peaks_unlocked()
            endpoints = {
                name: win.summary().to_dict()
                for name, win in sorted(self._endpoint_latency.items())
            }
            db_ops = {
                name: win.summary().to_dict()
                for name, win in sorted(self._db_op_latency.items())
            }
            payload: dict[str, Any] = {
                "ts": time.time(),
                "active_requests": self._active,
                "active_peak": self._active_peak,
                "in_flight": dict(self._in_flight),
                "endpoint_latency_ms": endpoints,
                "db_lock_wait_ms": self._db_lock_wait.summary().to_dict(),
                "db_lock_hold_ms": self._db_lock_hold.summary().to_dict(),
                "db_op_latency_ms": db_ops,
                "admitted": self._admitted,
                "rejected_503": self._rejected_503,
                "http_errors": self._http_errors,
                "thread_peak": self._thread_peak,
                "fd_peak": self._fd_peak,
                "thread_count": threading.active_count(),
                "fd_count": _fd_count(),
            }
            if persist:
                self._snapshots.append(dict(payload))
            return payload

    def recent_snapshots(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._snapshots)

    def persist_to_jsonl(self, path: Path, snapshot: dict[str, Any] | None = None) -> None:
        payload = snapshot if snapshot is not None else self.snapshot(persist=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, separators=(",", ":")) + "\n"
        with self._lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)


def _fd_count() -> int | None:
    try:
        return len(os.listdir(f"/proc/{os.getpid()}/fd"))
    except OSError:
        return None


def make_store_lock(telemetry: ControllerTelemetry | None) -> threading.RLock | InstrumentedRLock:
    if telemetry is None:
        return threading.RLock()
    return InstrumentedRLock(telemetry)
