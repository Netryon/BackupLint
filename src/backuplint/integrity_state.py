"""Local integrity verification state (no secrets)."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from backuplint.config import IntegrityMode


class IntegrityStateError(Exception):
    """User-facing integrity state-file failure."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class IntegrityStateRecord:
    last_success: datetime
    mode: IntegrityMode
    restic_version: str | None = None


def repository_state_key(repository: str) -> str:
    """Stable privacy-safe key for a repository location."""
    normalized = _normalize_repository(repository)
    return sha256(normalized.encode("utf-8")).hexdigest()


def _normalize_repository(repository: str) -> str:
    value = repository.strip()
    if "://" not in value:
        try:
            return str(Path(value).expanduser().resolve())
        except OSError:
            return value
    parts = urlsplit(value)
    # Drop userinfo so credentials never become identity material.
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _parse_timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def load_integrity_state(path: Path) -> dict[str, IntegrityStateRecord]:
    """Load integrity state records. Malformed files raise IntegrityStateError."""
    if not path.exists():
        return {}
    if not path.is_file():
        raise IntegrityStateError(f"Integrity state path is not a file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IntegrityStateError(
            f"Unable to read integrity state file: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise IntegrityStateError("Integrity state file root must be a mapping.")
    repos = payload.get("repositories", {})
    if not isinstance(repos, dict):
        raise IntegrityStateError("Integrity state 'repositories' must be a mapping.")

    records: dict[str, IntegrityStateRecord] = {}
    for key, raw in repos.items():
        if not isinstance(key, str) or not isinstance(raw, dict):
            raise IntegrityStateError("Integrity state repository entry is invalid.")
        ts = _parse_timestamp(raw.get("last_success"))
        mode_raw = raw.get("mode")
        if ts is None or not isinstance(mode_raw, str):
            raise IntegrityStateError("Integrity state repository entry is incomplete.")
        try:
            mode = IntegrityMode(mode_raw)
        except ValueError as exc:
            raise IntegrityStateError(
                f"Integrity state has invalid mode {mode_raw!r}."
            ) from exc
        version = raw.get("restic_version")
        if version is not None and not isinstance(version, str):
            raise IntegrityStateError("Integrity state restic_version must be a string.")
        records[key] = IntegrityStateRecord(
            last_success=ts,
            mode=mode,
            restic_version=version,
        )
    return records


def record_integrity_success(
    path: Path,
    *,
    repository: str,
    mode: IntegrityMode,
    checked_at: datetime | None = None,
    restic_version: str | None = None,
) -> None:
    """Atomically record a successful integrity verification."""
    when = checked_at or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    when = when.astimezone(UTC)

    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = load_integrity_state(path) if path.exists() else {}
    except IntegrityStateError:
        # Replace unusable state on successful verification.
        existing = {}

    key = repository_state_key(repository)
    existing[key] = IntegrityStateRecord(
        last_success=when,
        mode=mode,
        restic_version=restic_version,
    )
    payload = {
        "version": 1,
        "repositories": {
            repo_key: {
                "last_success": record.last_success.astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z"),
                "mode": record.mode.value,
                "restic_version": record.restic_version,
            }
            for repo_key, record in sorted(existing.items())
        },
    }
    _atomic_write_json(path, payload)


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    directory = path.parent
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(directory),
        )
    except OSError as exc:
        raise IntegrityStateError(
            f"Unable to write integrity state file: {path}"
        ) from exc
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except OSError as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise IntegrityStateError(
            f"Unable to write integrity state file: {path}"
        ) from exc


def last_success_for_repository(
    records: dict[str, IntegrityStateRecord],
    repository: str,
) -> IntegrityStateRecord | None:
    return records.get(repository_state_key(repository))


def is_integrity_success_stale(
    record: IntegrityStateRecord | None,
    *,
    max_age: timedelta,
    now: datetime | None = None,
) -> bool:
    """Return True when there is no usable success within max_age."""
    if record is None:
        return True
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    age = current.astimezone(UTC) - record.last_success.astimezone(UTC)
    if age.total_seconds() < 0:
        # Future timestamps are treated as untrusted/stale.
        return True
    return age > max_age
