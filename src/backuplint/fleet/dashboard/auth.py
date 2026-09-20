"""Dashboard operator authentication (session cookies; no SSO yet)."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from backuplint.fleet.dashboard.config import DashboardConfig

SESSION_COOKIE = "bl_dashboard_session"
CSRF_COOKIE = "bl_dashboard_csrf"
CSRF_HEADER = "X-CSRF-Token"


class DashboardAuthError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class _Session:
    token: str
    csrf: str
    expires_at: float


@dataclass
class _LoginBucket:
    failures: list[float]


class DashboardAuth:
    """Password + HttpOnly session auth for the read-only dashboard.

    Password verifier is stored as ``scrypt$salt$hex`` on disk (mode 0600).
    Sessions live in-process memory (lost on restart; operator re-logins).
    """

    def __init__(self, config: DashboardConfig, data_dir: Path) -> None:
        self.config = config
        self.hash_path = config.resolve_password_hash_path(data_dir)
        self._lock = threading.RLock()
        self._sessions: dict[str, _Session] = {}
        self._login_buckets: dict[str, _LoginBucket] = {}

    def password_configured(self) -> bool:
        return self.hash_path.is_file() and bool(self.hash_path.read_bytes().strip())

    def set_password(self, password: str) -> None:
        if len(password) < 12:
            raise DashboardAuthError("dashboard password must be at least 12 characters")
        if len(password.encode("utf-8")) > 256:
            raise DashboardAuthError("dashboard password too long")
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
        )
        self.hash_path.parent.mkdir(parents=True, exist_ok=True)
        payload = f"scrypt${salt.hex()}${digest.hex()}\n"
        tmp = self.hash_path.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.hash_path)
        os.chmod(self.hash_path, 0o600)

    def verify_password(self, password: str) -> bool:
        if not self.password_configured():
            return False
        raw = self.hash_path.read_text(encoding="utf-8").strip()
        try:
            algo, salt_hex, digest_hex = raw.split("$", 2)
        except ValueError:
            return False
        if algo != "scrypt":
            return False
        try:
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(digest_hex)
        except ValueError:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
        )
        return hmac.compare_digest(actual, expected)

    def _prune_sessions_locked(self, now: float) -> None:
        dead = [k for k, s in self._sessions.items() if s.expires_at <= now]
        for key in dead:
            del self._sessions[key]

    def create_session(self) -> tuple[str, str, int]:
        """Return (session_token, csrf_token, max_age_seconds)."""
        now = time.time()
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        with self._lock:
            self._prune_sessions_locked(now)
            self._sessions[token] = _Session(
                token=token,
                csrf=csrf,
                expires_at=now + self.config.session_ttl_seconds,
            )
        return token, csrf, self.config.session_ttl_seconds

    def destroy_session(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def get_session(self, token: str | None) -> _Session | None:
        if not token:
            return None
        now = time.time()
        with self._lock:
            self._prune_sessions_locked(now)
            session = self._sessions.get(token)
            if session is None:
                return None
            if session.expires_at <= now:
                del self._sessions[token]
                return None
            # Sliding expiry on activity.
            session.expires_at = now + self.config.session_ttl_seconds
            return session

    def check_csrf(self, session: _Session, provided: str | None) -> bool:
        if not provided:
            return False
        return hmac.compare_digest(session.csrf, provided)

    def allow_login_attempt(self, client_key: str) -> bool:
        now = time.time()
        window = float(self.config.login_rate_window_seconds)
        limit = int(self.config.login_rate_limit)
        with self._lock:
            self._prune_login_buckets_locked(now, window)
            bucket = self._login_buckets.get(client_key)
            if bucket is None:
                return True
            bucket.failures = [t for t in bucket.failures if now - t <= window]
            if not bucket.failures:
                del self._login_buckets[client_key]
                return True
            return len(bucket.failures) < limit

    def record_login_failure(self, client_key: str) -> None:
        now = time.time()
        window = float(self.config.login_rate_window_seconds)
        with self._lock:
            self._prune_login_buckets_locked(now, window)
            # Bound distinct-source growth (esp. IPv6) without dropping active limits.
            if (
                client_key not in self._login_buckets
                and len(self._login_buckets) >= 4096
            ):
                self._prune_login_buckets_locked(now, window, force_evict_to=2048)
            bucket = self._login_buckets.setdefault(client_key, _LoginBucket(failures=[]))
            bucket.failures.append(now)

    def _prune_login_buckets_locked(
        self, now: float, window: float, *, force_evict_to: int | None = None
    ) -> None:
        stale = [
            key
            for key, bucket in self._login_buckets.items()
            if not [t for t in bucket.failures if now - t <= window]
        ]
        for key in stale:
            del self._login_buckets[key]
        if force_evict_to is not None and len(self._login_buckets) > force_evict_to:
            # Evict oldest-by-most-recent-failure first.
            ranked = sorted(
                self._login_buckets.items(),
                key=lambda item: max(item[1].failures) if item[1].failures else 0.0,
            )
            for key, _bucket in ranked[: len(self._login_buckets) - force_evict_to]:
                del self._login_buckets[key]

    def clear_login_failures(self, client_key: str) -> None:
        with self._lock:
            self._login_buckets.pop(client_key, None)

    def cookie_header(
        self,
        name: str,
        value: str,
        *,
        max_age: int,
        http_only: bool,
    ) -> str:
        parts = [
            f"{name}={value}",
            "Path=/dashboard",
            f"Max-Age={max(0, int(max_age))}",
            f"SameSite={self.config.cookie_samesite}",
        ]
        # Also scope API cookies under /v1/dashboard via duplicate Path? Browser
        # cookies with Path=/dashboard would not send to /v1/dashboard. Use Path=/.
        parts[1] = "Path=/"
        if http_only:
            parts.append("HttpOnly")
        if self.config.cookie_secure:
            parts.append("Secure")
        return "; ".join(parts)

    def clear_cookie_header(self, name: str, *, http_only: bool) -> str:
        return self.cookie_header(name, "", max_age=0, http_only=http_only)
