"""Read-only Restic snapshot inspection and repository integrity helpers."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from backuplint.config import IntegrityMode
from backuplint.paths import find_covering_backup_path
from backuplint.process import TimeoutExpired, run_argv
from backuplint.restic_errors import (
    classify_integrity_operational,
    classify_list_snapshots_failure,
    integrity_error_message,
    list_snapshots_error_message,
)

# Integrity checks are heavier than snapshot listing.
STANDARD_INTEGRITY_TIMEOUT_SECONDS = 300.0
DEEP_INTEGRITY_TIMEOUT_SECONDS = 3600.0


class ResticError(Exception):
    """User-facing Restic inspection failure."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class IntegrityStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    STALE = "stale"


@dataclass(frozen=True)
class ResticSnapshot:
    snapshot_id: str
    short_id: str
    time: datetime | None
    paths: tuple[str, ...]


@dataclass(frozen=True)
class IntegrityCheckResult:
    mode: IntegrityMode
    status: IntegrityStatus
    duration_seconds: float
    message: str
    checked_at: datetime | None = None
    restic_version: str | None = None
    requested: bool = True


def _sanitize(text: str, secrets: tuple[str, ...]) -> str:
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "***")
    # Avoid leaking password-file contents if restic echoes paths only — still strip common markers.
    cleaned = cleaned.replace("wrong password", "authentication failed")
    # Lock messages may include hostname and local username.
    cleaned = re.sub(
        r"on \S+ by \S+(?: \(UID \d+, GID \d+\))?",
        "on *** by ***",
        cleaned,
    )
    return cleaned


def _parse_time(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        # Restic emits RFC3339 / ISO-8601 timestamps.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_snapshots_json(payload: str) -> list[ResticSnapshot]:
    """Parse `restic snapshots --json` output."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ResticError("Restic returned malformed JSON snapshot output.") from exc

    if data is None:
        return []
    if not isinstance(data, list):
        raise ResticError("Restic snapshot JSON must be a list.")

    snapshots: list[ResticSnapshot] = []
    for entry in data:
        if not isinstance(entry, dict):
            raise ResticError("Restic snapshot JSON contains a non-object entry.")
        paths_raw = entry.get("paths") or []
        if not isinstance(paths_raw, list):
            raise ResticError("Restic snapshot entry has invalid paths.")
        paths = tuple(str(path) for path in paths_raw if isinstance(path, str) and path)
        snapshot_id = str(entry.get("id") or "")
        short_id = str(entry.get("short_id") or snapshot_id[:8])
        snapshots.append(
            ResticSnapshot(
                snapshot_id=snapshot_id,
                short_id=short_id,
                time=_parse_time(entry.get("time")),
                paths=paths,
            )
        )
    return snapshots


def latest_snapshot(snapshots: list[ResticSnapshot]) -> ResticSnapshot | None:
    """Return the newest snapshot by timestamp, falling back to list order."""
    if not snapshots:
        return None
    dated = [item for item in snapshots if item.time is not None]
    if dated:
        return max(dated, key=lambda item: item.time or datetime.min)
    return snapshots[-1]


def latest_relevant_snapshot(
    snapshots: list[ResticSnapshot],
    data_path: str,
) -> ResticSnapshot | None:
    """Return the newest snapshot whose backup roots cover ``data_path``.

    Snapshots for unrelated roots are ignored so a newer ``/etc`` backup does
    not hide an older ``/srv/docker`` backup for mounts under ``/srv/docker``.
    """
    relevant: list[ResticSnapshot] = []
    for snapshot in snapshots:
        if find_covering_backup_path(data_path, snapshot.paths) is not None:
            relevant.append(snapshot)
    return latest_snapshot(relevant)


def _prepare_restic_env(
    *,
    repository: str,
    password: str | None = None,
    password_file: Path | None = None,
    password_ref: object | None = None,
) -> tuple[str, dict[str, str], tuple[str, ...]]:
    """Resolve restic binary, credential env, and secrets for scrubbing.

    Precedence (explicit wins; no silent cross-source fallback after a failure):
    1. ``password_ref`` (SecretRef from config)
    2. ``password_file`` (legacy path → file SecretRef)
    3. ``password`` keyword (tests / internal)
    4. ambient ``RESTIC_PASSWORD``
    5. ambient ``RESTIC_PASSWORD_FILE``
    """
    from backuplint.secrets import (
        SecretError,
        SecretRef,
        resolve_secret,
        secret_ref_from_file_path,
    )

    restic = shutil.which("restic")
    if restic is None:
        raise ResticError("Restic is not installed or not available on PATH.")

    if not repository.strip():
        raise ResticError("Restic repository path/URL is empty.")

    env = os.environ.copy()
    # Clear ambient password material first. If the config supplies credentials,
    # those must be the only secrets restic sees — otherwise a leftover
    # RESTIC_PASSWORD_FILE in the environment can override a wrong/missing
    # password_file and produce a false PASS.
    env.pop("RESTIC_PASSWORD", None)
    env.pop("RESTIC_PASSWORD_FILE", None)

    secrets: list[str] = []

    def _remember_secret(value: str | None) -> None:
        if value:
            secrets.append(value)

    def _apply_resolved(secret_text: str) -> None:
        env["RESTIC_PASSWORD"] = secret_text
        _remember_secret(secret_text)

    try:
        if password_ref is not None:
            if not isinstance(password_ref, SecretRef):
                raise ResticError("Invalid restic password secret reference.")
            _apply_resolved(resolve_secret(password_ref).get_secret_value())
        elif password_file is not None:
            ref = secret_ref_from_file_path(password_file)
            _apply_resolved(resolve_secret(ref).get_secret_value())
        elif password is not None:
            if not password:
                raise ResticError("Restic password is empty.")
            _apply_resolved(password)
        elif os.environ.get("RESTIC_PASSWORD"):
            _apply_resolved(os.environ["RESTIC_PASSWORD"])
        elif os.environ.get("RESTIC_PASSWORD_FILE"):
            ref = secret_ref_from_file_path(os.environ["RESTIC_PASSWORD_FILE"])
            # For ambient RESTIC_PASSWORD_FILE, keep restic-native file handoff
            # when possible, but still scrub using resolved value.
            secret_text = resolve_secret(ref).get_secret_value()
            env["RESTIC_PASSWORD"] = secret_text
            _remember_secret(secret_text)
        else:
            raise ResticError(
                "Restic password not provided. Set RESTIC_PASSWORD, "
                "RESTIC_PASSWORD_FILE, configure restic.password_file, "
                "or restic.password secret reference."
            )
    except SecretError as exc:
        # Never include resolved secret material; SecretError messages are safe.
        raise ResticError(exc.message) from exc

    env["RESTIC_REPOSITORY"] = repository
    return restic, env, tuple(secrets)


def list_snapshots(
    *,
    repository: str,
    password: str | None = None,
    password_file: Path | None = None,
    password_ref: object | None = None,
) -> list[ResticSnapshot]:
    """List snapshots from a Restic repository using JSON output only."""
    restic, env, secrets = _prepare_restic_env(
        repository=repository,
        password=password,
        password_file=password_file,
        password_ref=password_ref,
    )
    # Prefer env repository so the argv stays simple and free of secrets.
    command = [restic, "snapshots", "--json"]

    try:
        completed = run_argv(command, timeout=120, env=env)
    except TimeoutExpired as exc:
        raise ResticError("Restic snapshot listing timed out.") from exc
    except OSError as exc:
        raise ResticError(f"Failed to run Restic: {exc}") from exc

    if completed.returncode != 0:
        detail = _sanitize(
            (completed.stderr or completed.stdout or "unknown error").strip(),
            secrets,
        )
        kind = classify_list_snapshots_failure(detail)
        if kind is not None:
            raise ResticError(list_snapshots_error_message(kind, detail))
        raise ResticError(f"Restic snapshot listing failed:\n{detail}")

    return parse_snapshots_json(completed.stdout)


def _combined_output(stdout: str, stderr: str) -> str:
    parts = [part.strip() for part in (stderr, stdout) if part and part.strip()]
    return "\n".join(parts).strip()


def _classify_integrity_failure(
    *,
    mode: IntegrityMode,
    returncode: int,
    output: str,
    secrets: tuple[str, ...],
    duration_seconds: float,
) -> IntegrityCheckResult:
    """Map restic check failures to FAILED (corruption) or ERROR (operational)."""
    detail = _sanitize(output or "unknown error", secrets)
    kind = classify_integrity_operational(returncode=returncode, output=detail)
    if kind is not None:
        return IntegrityCheckResult(
            mode=mode,
            status=IntegrityStatus.ERROR,
            duration_seconds=duration_seconds,
            message=integrity_error_message(kind),
        )

    # Once credentials/access succeed, a non-zero `restic check` is treated as an
    # integrity failure even when stderr wording differs across Restic versions.
    return IntegrityCheckResult(
        mode=mode,
        status=IntegrityStatus.FAILED,
        duration_seconds=duration_seconds,
        message="Restic repository integrity check failed: repository inconsistency detected.",
    )


def probe_restic_version() -> str | None:
    """Return a short Restic version string when available."""
    restic = shutil.which("restic")
    if restic is None:
        return None
    try:
        completed = run_argv([restic, "version"], timeout=30, env=os.environ.copy())
    except (TimeoutExpired, OSError):
        return None
    if completed.returncode != 0:
        return None
    first = (completed.stdout or completed.stderr).strip().splitlines()
    if not first:
        return None
    # Example: "restic 0.18.1 compiled with go1.25.0 on linux/amd64"
    parts = first[0].split()
    if len(parts) >= 2 and parts[0].lower() == "restic":
        return parts[1]
    return first[0][:80]


def check_repository_integrity(
    *,
    mode: IntegrityMode,
    repository: str,
    password: str | None = None,
    password_file: Path | None = None,
    password_ref: object | None = None,
    timeout: float | None = None,
) -> IntegrityCheckResult:
    """Run a read-only Restic integrity check for the configured repository.

    Uses ``restic check`` for standard mode and ``restic check --read-data`` for
    deep mode. Does not repair, prune, unlock, or mutate repository contents.
    """
    checked_at = datetime.now(UTC)
    if mode is IntegrityMode.OFF:
        return IntegrityCheckResult(
            mode=mode,
            status=IntegrityStatus.NOT_REQUESTED,
            duration_seconds=0.0,
            message="Integrity verification was not requested.",
            checked_at=None,
            requested=False,
        )

    restic, env, secrets = _prepare_restic_env(
        repository=repository,
        password=password,
        password_file=password_file,
        password_ref=password_ref,
    )
    command = [restic, "check"]
    if mode is IntegrityMode.DEEP:
        command.append("--read-data")
        default_timeout = DEEP_INTEGRITY_TIMEOUT_SECONDS
        success_label = "deep data integrity check passed"  # noqa: S105  # nosec B105
    else:
        default_timeout = STANDARD_INTEGRITY_TIMEOUT_SECONDS
        success_label = "standard integrity check passed"  # noqa: S105  # nosec B105
    effective_timeout = default_timeout if timeout is None else timeout

    started = time.monotonic()
    try:
        completed = run_argv(command, timeout=effective_timeout, env=env)
    except TimeoutExpired as exc:
        raise ResticError(
            "Restic repository integrity check timed out."
        ) from exc
    except OSError as exc:
        raise ResticError(f"Failed to run Restic integrity check: {exc}") from exc

    elapsed = time.monotonic() - started
    version = probe_restic_version()
    if completed.returncode == 0:
        return IntegrityCheckResult(
            mode=mode,
            status=IntegrityStatus.PASSED,
            duration_seconds=elapsed,
            message=success_label,
            checked_at=checked_at,
            restic_version=version,
            requested=True,
        )

    classified = _classify_integrity_failure(
        mode=mode,
        returncode=completed.returncode,
        output=_combined_output(completed.stdout, completed.stderr),
        secrets=secrets,
        duration_seconds=elapsed,
    )
    return IntegrityCheckResult(
        mode=classified.mode,
        status=classified.status,
        duration_seconds=classified.duration_seconds,
        message=classified.message,
        checked_at=checked_at,
        restic_version=version,
        requested=True,
    )


DEFAULT_RESTORE_TIMEOUT_SECONDS = 1800.0


@dataclass(frozen=True)
class ResticRestoreResult:
    """Low-level outcome of a single ``restic restore`` invocation."""

    returncode: int
    duration_seconds: float
    stdout: str
    stderr: str
    sanitized_output: str


def restore_snapshot_paths(
    *,
    repository: str,
    snapshot_id: str,
    target: Path,
    include_paths: tuple[str, ...],
    password: str | None = None,
    password_file: Path | None = None,
    password_ref: object | None = None,
    timeout: float | None = None,
    verify: bool = True,
) -> ResticRestoreResult:
    """Restore selected absolute paths from a snapshot into ``target``.

    Uses argv-list execution only. Does not prune, forget, unlock, or repair.
    """
    if not snapshot_id.strip():
        raise ResticError("Restic snapshot id is empty.")
    if not include_paths:
        raise ResticError("No paths were provided for Restic restore.")

    restic, env, secrets = _prepare_restic_env(
        repository=repository,
        password=password,
        password_file=password_file,
        password_ref=password_ref,
    )
    command = [
        restic,
        "restore",
        snapshot_id.strip(),
        "--target",
        str(target),
    ]
    for include in include_paths:
        if not include.strip():
            raise ResticError("Restic restore include path is empty.")
        command.extend(["--include", include.strip()])
    if verify:
        command.append("--verify")

    effective_timeout = (
        DEFAULT_RESTORE_TIMEOUT_SECONDS if timeout is None else timeout
    )
    started = time.monotonic()
    try:
        completed = run_argv(command, timeout=effective_timeout, env=env)
    except TimeoutExpired as exc:
        raise ResticError("Restic restore timed out.") from exc
    except OSError as exc:
        raise ResticError(f"Failed to run Restic restore: {exc}") from exc

    elapsed = time.monotonic() - started
    raw = _combined_output(completed.stdout, completed.stderr)
    return ResticRestoreResult(
        returncode=completed.returncode,
        duration_seconds=elapsed,
        stdout=completed.stdout,
        stderr=completed.stderr,
        sanitized_output=_sanitize(raw, secrets),
    )
