"""Dashboard display helpers (status vocabulary, relative time, reasons)."""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

from backuplint.timeutil import format_relative_time

_STATUS_LABELS = {
    "PASS": "PASS",
    "WARN": "WARN",
    "FAIL": "FAIL",
    "ERROR": "ERROR",
    "NOT_RUN": "NOT RUN",
    "NOT RUN": "NOT RUN",
    "UNKNOWN": "UNKNOWN",
    "N/A": "N/A",
    "NA": "N/A",
    "NOT_APPLICABLE": "N/A",
    "NOT APPLICABLE": "N/A",
    "NONE": "N/A",
}

_PRESENCE_LABELS = {
    "online": "ONLINE",
    "ONLINE": "ONLINE",
    "stale": "STALE",
    "STALE": "STALE",
    "offline": "OFFLINE",
    "OFFLINE": "OFFLINE",
}

_STATUS_CLASS = {
    "PASS": "ok",
    "WARN": "warn",
    "FAIL": "fail",
    "ERROR": "error",
    "NOT RUN": "neutral",
    "UNKNOWN": "neutral",
    "N/A": "neutral",
    "ONLINE": "ok",
    "STALE": "warn",
    "OFFLINE": "fail",
}

_AGE_RE = re.compile(
    r"(?:latest relevant backup age|backup is|age)\s+(\d+)\s*"
    r"(m|min|minute|minutes|h|hour|hours|s|sec)?",
    re.IGNORECASE,
)
_THRESHOLD_RE = re.compile(
    r"threshold[:\s]+(\d+\s*(?:m|min|h|s)?)",
    re.IGNORECASE,
)


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def status_label(value: object, *, kind: str = "status") -> str:
    raw = "" if value is None else str(value).strip()
    if not raw or raw in {"None", "—", "-"}:
        return "N/A"
    if kind == "presence":
        return _PRESENCE_LABELS.get(raw, raw.upper().replace("_", " "))
    mapped = _STATUS_LABELS.get(raw) or _STATUS_LABELS.get(raw.upper().replace(" ", "_"))
    if mapped:
        return mapped
    return raw.replace("_", " ").upper()


def status_class(label: str) -> str:
    return _STATUS_CLASS.get(label, "neutral")


def status_tag(value: object, *, kind: str = "status") -> str:
    label = status_label(value, kind=kind)
    cls = status_class(label)
    return (
        f"<span class='tag tag-{_e(cls)}' role='status'>"
        f"<span class='tag-dot' aria-hidden='true'></span>"
        f"<span class='tag-label'>{_e(label)}</span>"
        "</span>"
    )


def relative_time_html(value: object, *, now: datetime | None = None) -> str:
    raw = "" if value is None else str(value).strip()
    if not raw or raw == "—":
        return "<span class='muted'>—</span>"
    rel = format_relative_time(raw, now=now)
    if not rel:
        return f"<span class='when' title='{_e(raw)}'>{_e(raw)}</span>"
    return (
        f"<time class='when' datetime='{_e(raw)}' title='{_e(raw)}'>{_e(rel)}</time>"
    )


def warn_reason_html(
    state: object,
    detail: object,
    *,
    last_snapshot: object | None = None,
) -> str:
    """Human-readable warning copy without inventing product semantics."""
    label = status_label(state)
    text = "" if detail is None else str(detail).strip()
    snap = "" if last_snapshot is None else str(last_snapshot).strip()
    parts: list[str] = []
    if label == "WARN" and text:
        age = _AGE_RE.search(text)
        if age:
            unit = (age.group(2) or "m")[0]
            parts.append(f"Backup is {age.group(1)}{unit} old")
            thresh = _THRESHOLD_RE.search(text)
            if thresh:
                parts.append(f"Threshold: {thresh.group(1).strip()}")
            remainder = _AGE_RE.sub("", text).strip(" .;,-")
            remainder = _THRESHOLD_RE.sub("", remainder).strip(" .;,-")
            if remainder and remainder.lower() not in {"warn", "warning"}:
                parts.append(remainder)
        else:
            parts.append(text)
    elif text:
        parts.append(text)
    if snap and snap not in " ".join(parts):
        parts.append(f"Last backup {snap}")
    if not parts:
        return ""
    return "<div class='reason'>" + "<br/>".join(_e(p) for p in parts) + "</div>"


def is_fleet_only(capability_summary: dict[str, Any] | None) -> bool:
    if not isinstance(capability_summary, dict):
        return False
    families = capability_summary.get("families")
    if not isinstance(families, dict) or not families:
        return False
    fleet = families.get("fleet_reporting") or {}
    docker = families.get("docker_compose") or families.get("coverage_scanning") or {}
    restic = families.get("restic") or {}
    fleet_ok = isinstance(fleet, dict) and bool(fleet.get("available"))
    local_missing = (
        isinstance(docker, dict) and docker.get("available") is False
    ) or (isinstance(restic, dict) and restic.get("available") is False)
    return bool(fleet_ok and local_missing)


def deployment_form_label(value: object) -> str:
    raw = "" if value is None else str(value).strip().lower()
    if raw == "native":
        return "Native agent"
    if raw == "container":
        return "Container agent"
    if raw:
        return raw.replace("_", " ").title()
    return "Agent"
