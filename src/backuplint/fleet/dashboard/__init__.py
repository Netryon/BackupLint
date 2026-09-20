"""Optional read-only-first central dashboard (v0.6)."""

from __future__ import annotations

from backuplint.fleet.dashboard.auth import DashboardAuth, DashboardAuthError
from backuplint.fleet.dashboard.config import DashboardConfig
from backuplint.fleet.dashboard.query import DashboardQueryService

__all__ = [
    "DashboardAuth",
    "DashboardAuthError",
    "DashboardConfig",
    "DashboardQueryService",
]
