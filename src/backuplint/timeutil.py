"""Parse human-readable durations for backup freshness checks."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_DURATION_RE = re.compile(
    r"^\s*(?:(?P<days>\d+)\s*d)?\s*(?:(?P<hours>\d+)\s*h)?\s*"
    r"(?:(?P<minutes>\d+)\s*m)?\s*(?:(?P<seconds>\d+)\s*s)?\s*$",
    re.IGNORECASE,
)


def parse_duration(value: str) -> timedelta:
    """Parse values like ``24h``, ``7d``, ``90m``, or ``1d12h``.

    At least one unit must be present. Empty or invalid strings raise ValueError.
    """
    text = value.strip()
    if not text:
        raise ValueError("duration is empty")

    # Allow a single bare unit form such as 24h / 7d.
    match = _DURATION_RE.fullmatch(text)
    if match is None:
        raise ValueError(f"invalid duration: {value!r}")

    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    if days == hours == minutes == seconds == 0:
        raise ValueError(f"invalid duration: {value!r}")
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def format_relative_time(
    value: object,
    *,
    now: datetime | None = None,
) -> str:
    """Compact relative time for dense tables (``2m ago``). Empty if unparsable."""
    from datetime import UTC

    if value is None:
        return ""
    text = str(value).strip()
    if not text or text == "—":
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    current = now if now is not None else datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    delta = current - parsed.astimezone(UTC)
    seconds = int(delta.total_seconds())
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 45:
        label = "just now" if not future else "soon"
        return label
    minutes, _ = divmod(seconds, 60)
    hours, minutes_r = divmod(minutes, 60)
    days, hours_r = divmod(hours, 24)
    if days:
        core = f"{days}d"
    elif hours:
        core = f"{hours}h"
    elif minutes:
        core = f"{minutes}m"
    else:
        core = f"{seconds}s"
    if future:
        return f"in {core}"
    return f"{core} ago"


def format_age(delta: timedelta) -> str:
    """Format a non-negative age for human-readable audit output."""
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        total_seconds = 0
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)
