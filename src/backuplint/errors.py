"""Shared user-facing exception types."""

from __future__ import annotations


class ConfigError(Exception):
    """User-facing configuration failure."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
