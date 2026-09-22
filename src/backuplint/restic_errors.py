"""Shared classification of operational Restic failures (internal).

Consolidates phrase matching used by snapshot listing, integrity checks, and
restore verification while preserving historical user-facing message strings.
"""

from __future__ import annotations

from enum import StrEnum


class ResticOperationalKind(StrEnum):
    """Internal operational failure categories (not a public JSON contract)."""

    AUTH = "auth"
    LOCKED = "locked"
    NOT_FOUND = "not_found"
    NETWORK = "network"
    PERMISSION = "permission"


_NETWORK_PHRASES = (
    "connection refused",
    "connection reset",
    "broken pipe",
    "network is unreachable",
    "no route to host",
    "i/o timeout",
    "dial tcp",
    "connect: ",
    "temporary failure in name resolution",
)

_RESTORE_NETWORK_EXTRA = (
    "tls handshake timeout",
    "context deadline exceeded",
    "server misbehaving",
)


def _has_any(lower: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in lower for phrase in phrases)


def classify_list_snapshots_failure(output: str) -> ResticOperationalKind | None:
    """Operational kinds for ``restic snapshots`` failures (historical rules)."""
    lower = (output or "").lower()
    if "password" in lower or "decrypt" in lower or "authentication" in lower:
        return ResticOperationalKind.AUTH
    if (
        "no such file" in lower
        or "does not exist" in lower
        or "is not a repository" in lower
    ):
        return ResticOperationalKind.NOT_FOUND
    if "already locked" in lower or "unable to create lock" in lower:
        return ResticOperationalKind.LOCKED
    if _has_any(lower, _NETWORK_PHRASES):
        return ResticOperationalKind.NETWORK
    if not lower.strip():
        return ResticOperationalKind.LOCKED
    return None


def classify_integrity_operational(
    *,
    returncode: int,
    output: str,
) -> ResticOperationalKind | None:
    """Operational kinds for ``restic check`` failures (historical rules)."""
    lower = (output or "").lower()
    if returncode != 0 and not lower.strip():
        return ResticOperationalKind.LOCKED
    if returncode == 12 or (
        "wrong password" in lower
        or "authentication failed" in lower
        or "no key found" in lower
    ):
        return ResticOperationalKind.AUTH
    if returncode == 11 or "already locked" in lower or "unable to create lock" in lower:
        return ResticOperationalKind.LOCKED
    if returncode == 10 or (
        "repository does not exist" in lower
        or "unable to open config file" in lower
        or "is not a repository" in lower
        or (
            "no such file" in lower
            and "config" in lower
            and "not found in repository" not in lower
            and "error loading index" not in lower
            and "loadraw" not in lower
        )
    ):
        return ResticOperationalKind.NOT_FOUND
    if _has_any(lower, _NETWORK_PHRASES):
        return ResticOperationalKind.NETWORK
    return None


def classify_restore_operational(
    *,
    returncode: int,
    output: str,
) -> ResticOperationalKind | None:
    """Operational kinds for ``restic restore`` failures (historical rules)."""
    lower = (output or "").lower()
    if returncode == 12 or (
        "wrong password" in lower
        or "authentication failed" in lower
        or "no key found" in lower
    ):
        return ResticOperationalKind.AUTH
    if returncode == 11 or "already locked" in lower or "unable to create lock" in lower:
        return ResticOperationalKind.LOCKED
    if returncode == 10 or (
        "repository does not exist" in lower
        or "unable to open config file" in lower
        or "is not a repository" in lower
        or ("no such file" in lower and "config" in lower)
    ):
        return ResticOperationalKind.NOT_FOUND
    if "permission denied" in lower:
        return ResticOperationalKind.PERMISSION
    if _has_any(lower, _NETWORK_PHRASES + _RESTORE_NETWORK_EXTRA):
        return ResticOperationalKind.NETWORK
    return None


def list_snapshots_error_message(kind: ResticOperationalKind, detail: str) -> str:
    if kind is ResticOperationalKind.AUTH:
        return "Unable to open Restic repository (authentication failed)."
    if kind is ResticOperationalKind.NOT_FOUND:
        return "Unable to open Restic repository (inaccessible or missing)."
    if kind is ResticOperationalKind.NETWORK:
        return (
            "Unable to open Restic repository (backend unavailable or network failure)."
        )
    if kind is ResticOperationalKind.LOCKED:
        return "Unable to open Restic repository (repository is locked)."
    return f"Restic snapshot listing failed:\n{detail}"


def integrity_error_message(kind: ResticOperationalKind) -> str:
    if kind is ResticOperationalKind.AUTH:
        return "Unable to verify Restic repository integrity (authentication failed)."
    if kind is ResticOperationalKind.LOCKED:
        return "Unable to verify Restic repository integrity (repository is locked)."
    if kind is ResticOperationalKind.NOT_FOUND:
        return "Unable to verify Restic repository integrity (inaccessible or missing)."
    if kind is ResticOperationalKind.NETWORK:
        return (
            "Unable to verify Restic repository integrity "
            "(backend unavailable or network failure)."
        )
    return "Unable to verify Restic repository integrity (operational failure)."


def restore_error_message(kind: ResticOperationalKind) -> str:
    if kind is ResticOperationalKind.AUTH:
        return "Unable to complete restore verification (authentication failed)."
    if kind is ResticOperationalKind.LOCKED:
        return "Unable to complete restore verification (repository is locked)."
    if kind is ResticOperationalKind.NOT_FOUND:
        return (
            "Unable to complete restore verification "
            "(inaccessible or missing repository)."
        )
    if kind is ResticOperationalKind.PERMISSION:
        return "Unable to complete restore verification (permission denied)."
    if kind is ResticOperationalKind.NETWORK:
        return (
            "Unable to complete restore verification "
            "(backend unavailable or network failure)."
        )
    return "Unable to complete restore verification (operational failure)."
