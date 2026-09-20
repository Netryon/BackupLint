"""Secret providers and resolution lifecycle."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from backuplint.secrets.errors import (
    SecretMissingError,
    SecretResolutionError,
    SecretSourceUnavailableError,
)
from backuplint.secrets.file_checks import (
    DEFAULT_MAX_SECRET_BYTES,
    credentials_directory,
    read_secret_file,
)
from backuplint.secrets.ref import SecretRef, SecretSource
from backuplint.secrets.value import SecretValue


@dataclass(frozen=True)
class ResolveContext:
    """Resolution environment (never logs values)."""

    environ: Mapping[str, str]
    credentials_dir: Path | None = None
    max_bytes: int = DEFAULT_MAX_SECRET_BYTES
    require_owner_only: bool = True


class SecretProvider(Protocol):
    """Narrow extension point for future external secret stores."""

    name: str

    def supports(self, ref: SecretRef) -> bool: ...

    def resolve(self, ref: SecretRef, *, context: ResolveContext) -> SecretValue: ...


class FileSecretProvider:
    name = "file"

    def supports(self, ref: SecretRef) -> bool:
        return ref.source in {SecretSource.FILE, SecretSource.MOUNTED}

    def resolve(self, ref: SecretRef, *, context: ResolveContext) -> SecretValue:
        if ref.path is None:
            raise SecretResolutionError("file secret reference missing path")
        text = read_secret_file(
            Path(ref.path),
            max_bytes=context.max_bytes,
            require_owner_only=context.require_owner_only,
        )
        return SecretValue(text)


class EnvSecretProvider:
    name = "env"

    def supports(self, ref: SecretRef) -> bool:
        return ref.source is SecretSource.ENV

    def resolve(self, ref: SecretRef, *, context: ResolveContext) -> SecretValue:
        if ref.name is None:
            raise SecretResolutionError("env secret reference missing name")
        if ref.name not in context.environ:
            raise SecretMissingError(f"environment variable {ref.name!r} is unset")
        value = context.environ[ref.name]
        # Empty string is distinct from unset: treat as missing for credentials.
        if value == "":
            raise SecretMissingError(f"environment variable {ref.name!r} is empty")
        if len(value.encode("utf-8")) > context.max_bytes:
            from backuplint.secrets.errors import SecretTooLargeError

            raise SecretTooLargeError("environment secret exceeds maximum size")
        return SecretValue(value)


class SystemdCredentialProvider:
    name = "systemd"

    def supports(self, ref: SecretRef) -> bool:
        return ref.source is SecretSource.SYSTEMD

    def resolve(self, ref: SecretRef, *, context: ResolveContext) -> SecretValue:
        if ref.name is None:
            raise SecretResolutionError("systemd secret reference missing name")
        cred_dir = context.credentials_dir
        if cred_dir is None:
            cred_dir = credentials_directory()
        if cred_dir is None:
            raise SecretSourceUnavailableError(
                "systemd credentials unavailable "
                "(CREDENTIALS_DIRECTORY not set or not a directory)"
            )
        # Name already validated as basename in parse_secret_ref.
        path = (cred_dir / ref.name).resolve()
        try:
            path.relative_to(cred_dir.resolve())
        except ValueError as exc:
            raise SecretResolutionError(
                "systemd credential path escapes credentials directory"
            ) from exc
        text = read_secret_file(
            path,
            max_bytes=context.max_bytes,
            require_owner_only=context.require_owner_only,
        )
        return SecretValue(text)


DEFAULT_PROVIDERS: tuple[SecretProvider, ...] = (
    FileSecretProvider(),
    EnvSecretProvider(),
    SystemdCredentialProvider(),
)


def resolve_secret(
    ref: SecretRef,
    *,
    environ: Mapping[str, str] | None = None,
    credentials_dir: Path | None = None,
    providers: Sequence[SecretProvider] | None = None,
    require_owner_only: bool = True,
    max_bytes: int = DEFAULT_MAX_SECRET_BYTES,
) -> SecretValue:
    """Resolve a validated SecretRef to a SecretValue (late binding)."""
    context = ResolveContext(
        environ=environ if environ is not None else os.environ,
        credentials_dir=credentials_dir,
        max_bytes=max_bytes,
        require_owner_only=require_owner_only,
    )
    chain = providers if providers is not None else DEFAULT_PROVIDERS
    for provider in chain:
        if provider.supports(ref):
            return provider.resolve(ref, context=context)
    raise SecretSourceUnavailableError(
        f"no provider available for secret source {ref.source.value!r}"
    )
