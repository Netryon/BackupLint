"""Dashboard runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DashboardConfig:
    """Controller-side dashboard settings.

    Dashboard is optional. When ``enabled`` is false, routes return 404.
    """

    enabled: bool = False
    password_hash_path: Path | None = None
    session_ttl_seconds: int = 8 * 3600
    online_after_seconds: int = 120
    stale_after_seconds: int = 600
    max_page_size: int = 100
    default_page_size: int = 50
    login_rate_limit: int = 20
    login_rate_window_seconds: int = 300
    cookie_secure: bool = True
    cookie_samesite: str = "Strict"

    def resolve_password_hash_path(self, data_dir: Path) -> Path:
        if self.password_hash_path is not None:
            return self.password_hash_path
        return data_dir / "dashboard" / "password.scrypt"
