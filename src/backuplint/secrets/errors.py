"""Secret reference errors that never embed resolved secret values."""

from __future__ import annotations


class SecretError(Exception):
    """Base class for credential-source failures."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidSecretRefError(SecretError):
    """Secret reference shape/type is invalid."""


class SecretSourceUnavailableError(SecretError):
    """Configured source cannot be used in this environment."""


class SecretMissingError(SecretError):
    """Referenced secret does not exist / is unset."""


class SecretPermissionError(SecretError):
    """Secret file permissions or ownership are unsafe."""


class SecretTooLargeError(SecretError):
    """Secret exceeds the allowed maximum size."""


class SecretResolutionError(SecretError):
    """Resolution failed for a reason not covered more specifically."""
