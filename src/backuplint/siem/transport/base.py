"""SIEM transport protocol and result classification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from backuplint.siem.config import SiemConfig
from backuplint.siem.event import SiemEvent


@dataclass(frozen=True)
class TransportResult:
    delivered: bool
    retryable: bool
    status_code: int | None
    retry_after_seconds: float | None
    error: str | None


class Transport(Protocol):
    name: str

    def send(self, event: SiemEvent, *, config: SiemConfig) -> TransportResult: ...


def classify_http_status(
    status_code: int,
    *,
    retry_after_seconds: float | None = None,
) -> TransportResult:
    if 200 <= status_code < 300:
        return TransportResult(
            delivered=True,
            retryable=False,
            status_code=status_code,
            retry_after_seconds=None,
            error=None,
        )
    if status_code in (401, 403):
        return TransportResult(
            delivered=False,
            retryable=True,
            status_code=status_code,
            retry_after_seconds=retry_after_seconds,
            error=f"HTTP {status_code} authentication failure",
        )
    if status_code == 429:
        return TransportResult(
            delivered=False,
            retryable=True,
            status_code=status_code,
            retry_after_seconds=retry_after_seconds,
            error="HTTP 429 rate limited",
        )
    if 500 <= status_code < 600:
        return TransportResult(
            delivered=False,
            retryable=True,
            status_code=status_code,
            retry_after_seconds=retry_after_seconds,
            error=f"HTTP {status_code} server error",
        )
    if 300 <= status_code < 400:
        return TransportResult(
            delivered=False,
            retryable=True,
            status_code=status_code,
            retry_after_seconds=None,
            error="redirect not followed",
        )
    return TransportResult(
        delivered=False,
        retryable=True,
        status_code=status_code,
        retry_after_seconds=retry_after_seconds,
        error=f"HTTP {status_code} unexpected response",
    )
