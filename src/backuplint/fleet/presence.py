"""Shared fleet presence classification (online/stale/offline)."""

from __future__ import annotations

from datetime import UTC, datetime

ONLINE = "online"
STALE = "stale"
OFFLINE = "offline"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def classify_presence(
    last_heartbeat: str | None,
    *,
    now: datetime | None = None,
    online_after_seconds: int,
    stale_after_seconds: int,
) -> str:
    """Map heartbeat freshness to online/stale/offline (not audit health)."""
    hb = _parse_iso(last_heartbeat)
    if hb is None:
        return OFFLINE
    current = now or datetime.now(UTC)
    age = (current - hb).total_seconds()
    if age <= online_after_seconds:
        return ONLINE
    if age <= stale_after_seconds:
        return STALE
    return OFFLINE
