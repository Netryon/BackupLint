"""BackupLint-owned policy settings validation (v0.8).

Rejects dangerous fields, raw secrets, shell metacharacters in executable
fields, and absolute paths outside managed BackupLint state.
"""

from __future__ import annotations

import re
from typing import Any

from backuplint.policy.errors import PolicyError
from backuplint.secrets import InvalidSecretRefError, parse_secret_ref

_ALLOWED_TOP_LEVEL = frozenset(
    {
        "schedule",
        "siem",
        "queue",
        "reporting",
    }
)

_SECRET_REF_FIELD_NAMES = frozenset({"auth_token"})

_FORBIDDEN_KEY_PATTERNS = (
    re.compile(r"(?i)(password|secret|private_key|api_key|bearer)"),
    re.compile(r"(?i)(^|_)token$"),
    re.compile(r"(?i)^(ssh_|docker_|firewall_|package_|systemd_)"),
)

_SHELL_METACHAR_RE = re.compile(r"[;|&$`<>\\]")

_FORBIDDEN_VALUE_PATTERNS = (
    re.compile(r"(?i)restic.*password"),
    re.compile(r"(?i)s3.*secret"),
)

_DURATION_RE = re.compile(
    r"^\d+[smhdw](?:\s+\d+[smhdw])*$|^\d+\s*(?:second|minute|hour|day|week)s?$",
    re.IGNORECASE,
)

_SCHEDULE_CHECKS = frozenset(
    {"coverage", "integrity", "deep_integrity", "restore_verification"}
)


def _reject_key(key: str, *, path: str) -> None:
    if key in _SECRET_REF_FIELD_NAMES:
        return
    for pattern in _FORBIDDEN_KEY_PATTERNS:
        if pattern.search(key):
            raise PolicyError(f"forbidden field at {path}.{key}")
    lowered = key.lower()
    if lowered in {"command", "exec", "shell", "script", "docker", "ssh", "firewall"}:
        raise PolicyError(f"forbidden field at {path}.{key}")


def _reject_string_value(value: str, *, path: str, allow_duration: bool = False) -> None:
    if "\x00" in value:
        raise PolicyError(f"NUL byte in {path}")
    if len(value) > 4096:
        raise PolicyError(f"value too long at {path}")
    if _SHELL_METACHAR_RE.search(value):
        raise PolicyError(f"shell metacharacters rejected at {path}")
    if value.startswith("/") or ".." in value.split("/"):
        raise PolicyError(f"absolute or traversal path rejected at {path}")
    for pattern in _FORBIDDEN_VALUE_PATTERNS:
        if pattern.search(value):
            raise PolicyError(f"forbidden secret-like value at {path}")
    if not allow_duration and _looks_like_raw_secret(value):
        raise PolicyError(f"raw secret-like value rejected at {path}")


def _looks_like_raw_secret(value: str) -> bool:
    if len(value) < 8:
        return False
    if value.startswith("sk-") or value.startswith("AKIA"):
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9+/=_-]{32,}", value))


def _validate_duration(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyError(f"{path} must be a duration string")
    text = value.strip()
    if not _DURATION_RE.match(text):
        raise PolicyError(f"invalid duration at {path}")
    return text


def _validate_schedule(settings: object) -> dict[str, object]:
    if not isinstance(settings, dict):
        raise PolicyError("schedule must be an object")
    out: dict[str, object] = {}
    for key, raw in settings.items():
        if key not in _SCHEDULE_CHECKS:
            raise PolicyError(f"unknown schedule check {key!r}")
        if not isinstance(raw, dict):
            raise PolicyError(f"schedule.{key} must be an object")
        job: dict[str, object] = {}
        if "enabled" in raw:
            if not isinstance(raw["enabled"], bool):
                raise PolicyError(f"schedule.{key}.enabled must be boolean")
            job["enabled"] = raw["enabled"]
        if "every" in raw:
            job["every"] = _validate_duration(raw["every"], f"schedule.{key}.every")
        out[key] = job
    return out


def _validate_siem(settings: object) -> dict[str, object]:
    if not isinstance(settings, dict):
        raise PolicyError("siem must be an object")
    out: dict[str, object] = {}
    if "enabled" in settings:
        if not isinstance(settings["enabled"], bool):
            raise PolicyError("siem.enabled must be boolean")
        out["enabled"] = settings["enabled"]
    if "endpoint" in settings:
        ep = settings["endpoint"]
        if not isinstance(ep, str):
            raise PolicyError("siem.endpoint must be a string")
        text = ep.strip()
        if not text.lower().startswith("https://"):
            raise PolicyError("siem.endpoint must be an https:// URL")
        if "\x00" in text or len(text) > 4096:
            raise PolicyError("invalid siem.endpoint")
        if _SHELL_METACHAR_RE.search(text):
            raise PolicyError("shell metacharacters rejected at siem.endpoint")
        if ".." in text.split("/"):
            raise PolicyError("traversal path rejected at siem.endpoint")
        out["endpoint"] = text
    if "auth_token" in settings:
        try:
            ref = parse_secret_ref(settings["auth_token"], field_name="siem.auth_token")
        except InvalidSecretRefError as exc:
            raise PolicyError(str(exc)) from exc
        out["auth_token"] = ref.to_mapping()
    if "min_severity" in settings:
        sev = settings["min_severity"]
        if not isinstance(sev, str) or sev not in {
            "info",
            "low",
            "medium",
            "high",
            "critical",
        }:
            raise PolicyError("siem.min_severity invalid")
        out["min_severity"] = sev
    return out


def _validate_queue(settings: object) -> dict[str, object]:
    if not isinstance(settings, dict):
        raise PolicyError("queue must be an object")
    out: dict[str, object] = {}
    for key in ("max_items", "reserved_critical"):
        if key in settings:
            val = settings[key]
            if not isinstance(val, int) or val < 1 or val > 10000:
                raise PolicyError(f"queue.{key} must be integer 1..10000")
            out[key] = val
    return out


def _validate_reporting(settings: object) -> dict[str, object]:
    if not isinstance(settings, dict):
        raise PolicyError("reporting must be an object")
    out: dict[str, object] = {}
    for key in ("heartbeat_interval", "policy_poll_interval"):
        if key in settings:
            out[key] = _validate_duration(settings[key], f"reporting.{key}")
    return out


def _walk_unknown(obj: object, path: str) -> None:
    if isinstance(obj, dict):
        if "source" in obj and isinstance(obj.get("source"), str):
            return
        for key, value in obj.items():
            if not isinstance(key, str):
                raise PolicyError(f"non-string key at {path}")
            _reject_key(key, path=path)
            _walk_unknown(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            _walk_unknown(item, f"{path}[{idx}]")
    elif isinstance(obj, str):
        if obj.lower().startswith("https://"):
            if _SHELL_METACHAR_RE.search(obj) or ".." in obj.split("/"):
                raise PolicyError(f"invalid URL at {path}")
            return
        _reject_string_value(obj, path=path)
    elif isinstance(obj, (bool, int, float)) or obj is None:
        return
    else:
        raise PolicyError(f"unsupported value type at {path}")


def validate_policy_settings(settings: dict[str, Any]) -> dict[str, object]:
    """Validate and normalize BackupLint-owned policy settings."""
    if not isinstance(settings, dict):
        raise PolicyError("settings must be an object")
    for key in settings:
        if key not in _ALLOWED_TOP_LEVEL:
            raise PolicyError(f"unknown settings key {key!r}")
        _reject_key(key, path="settings")
    out: dict[str, object] = {}
    if "schedule" in settings:
        out["schedule"] = _validate_schedule(settings["schedule"])
    if "siem" in settings:
        out["siem"] = _validate_siem(settings["siem"])
    if "queue" in settings:
        out["queue"] = _validate_queue(settings["queue"])
    if "reporting" in settings:
        out["reporting"] = _validate_reporting(settings["reporting"])
    _walk_unknown(out, "settings")
    return out
