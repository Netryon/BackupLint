"""Secrets / credential-source abstraction."""

from __future__ import annotations

from backuplint.secrets.errors import (
    InvalidSecretRefError,
    SecretError,
    SecretMissingError,
    SecretPermissionError,
    SecretResolutionError,
    SecretSourceUnavailableError,
    SecretTooLargeError,
)
from backuplint.secrets.file_checks import (
    DEFAULT_MAX_SECRET_BYTES,
    SensitiveFileReport,
    credentials_directory,
    diagnose_sensitive_file,
    read_secret_file,
)
from backuplint.secrets.ref import (
    SecretRef,
    SecretSource,
    parse_secret_ref,
    secret_ref_from_file_path,
    secret_ref_roundtrip,
)
from backuplint.secrets.resolve import (
    DEFAULT_PROVIDERS,
    EnvSecretProvider,
    FileSecretProvider,
    ResolveContext,
    SecretProvider,
    SystemdCredentialProvider,
    resolve_secret,
)
from backuplint.secrets.value import SecretValue

__all__ = [
    "DEFAULT_MAX_SECRET_BYTES",
    "DEFAULT_PROVIDERS",
    "EnvSecretProvider",
    "FileSecretProvider",
    "InvalidSecretRefError",
    "ResolveContext",
    "SecretError",
    "SecretMissingError",
    "SecretPermissionError",
    "SecretProvider",
    "SecretRef",
    "SecretResolutionError",
    "SecretSource",
    "SecretSourceUnavailableError",
    "SecretTooLargeError",
    "SecretValue",
    "SensitiveFileReport",
    "SystemdCredentialProvider",
    "credentials_directory",
    "diagnose_sensitive_file",
    "parse_secret_ref",
    "read_secret_file",
    "resolve_secret",
    "secret_ref_from_file_path",
    "secret_ref_roundtrip",
]
