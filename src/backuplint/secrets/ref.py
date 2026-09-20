"""Canonical SecretRef model: parse/validate/serialize without resolving."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from backuplint.secrets.errors import InvalidSecretRefError


class SecretSource(StrEnum):
    FILE = "file"
    ENV = "env"
    SYSTEMD = "systemd"
    MOUNTED = "mounted"


_ALLOWED_SOURCES = {s.value for s in SecretSource}


@dataclass(frozen=True)
class SecretRef:
    """Reference metadata only — never holds a resolved secret value."""

    source: SecretSource
    path: str | None = None
    name: str | None = None

    def to_mapping(self) -> dict[str, str]:
        payload: dict[str, str] = {"source": self.source.value}
        if self.path is not None:
            payload["path"] = self.path
        if self.name is not None:
            payload["name"] = self.name
        return payload

    def to_json_dict(self) -> dict[str, str]:
        return self.to_mapping()


def parse_secret_ref(
    raw: object,
    *,
    config_dir: Path | None = None,
    field_name: str = "secret",
) -> SecretRef:
    """Parse and validate a secret reference mapping (does not read secrets)."""
    if isinstance(raw, str):
        raise InvalidSecretRefError(
            f"{field_name} must be a mapping with 'source' "
            "(inline raw secret strings are not accepted)."
        )
    if not isinstance(raw, dict):
        raise InvalidSecretRefError(f"{field_name} must be a mapping.")

    unknown = sorted(set(raw) - {"source", "path", "name"})
    if unknown:
        raise InvalidSecretRefError(
            f"{field_name} has unknown key(s): "
            + ", ".join(repr(k) for k in unknown)
        )

    source_raw = raw.get("source")
    if not isinstance(source_raw, str) or not source_raw.strip():
        raise InvalidSecretRefError(f"{field_name}.source must be a non-empty string.")
    source_key = source_raw.strip().lower()
    if source_key not in _ALLOWED_SOURCES:
        raise InvalidSecretRefError(
            f"{field_name}.source must be one of: "
            + ", ".join(sorted(_ALLOWED_SOURCES))
        )
    source = SecretSource(source_key)

    path_raw = raw.get("path")
    name_raw = raw.get("name")
    path: str | None = None
    name: str | None = None

    if path_raw is not None:
        if not isinstance(path_raw, str) or not path_raw.strip():
            raise InvalidSecretRefError(f"{field_name}.path must be a non-empty string.")
        path = path_raw.strip()
        if "\x00" in path:
            raise InvalidSecretRefError(f"{field_name}.path contains NUL.")
    if name_raw is not None:
        if not isinstance(name_raw, str) or not name_raw.strip():
            raise InvalidSecretRefError(f"{field_name}.name must be a non-empty string.")
        name = name_raw.strip()
        if "\x00" in name or "\n" in name or "\r" in name:
            raise InvalidSecretRefError(f"{field_name}.name contains invalid characters.")

    if source is SecretSource.FILE:
        if path is None:
            raise InvalidSecretRefError(f"{field_name} file source requires 'path'.")
        if name is not None:
            raise InvalidSecretRefError(f"{field_name} file source does not use 'name'.")
        if config_dir is not None and not Path(path).is_absolute():
            path = str((config_dir / path).expanduser())
        else:
            path = str(Path(path).expanduser())
    elif source is SecretSource.ENV:
        if name is None:
            raise InvalidSecretRefError(f"{field_name} env source requires 'name'.")
        if path is not None:
            raise InvalidSecretRefError(f"{field_name} env source does not use 'path'.")
    elif source is SecretSource.SYSTEMD:
        if name is None:
            raise InvalidSecretRefError(
                f"{field_name} systemd source requires credential 'name'."
            )
        if path is not None:
            raise InvalidSecretRefError(
                f"{field_name} systemd source does not use 'path'."
            )
        if "/" in name or name in {".", ".."} or "\\" in name:
            raise InvalidSecretRefError(
                f"{field_name} systemd credential name must be a plain basename."
            )
    elif source is SecretSource.MOUNTED:
        # Docker/Podman-compatible mounted secret file.
        if path is None and name is None:
            raise InvalidSecretRefError(
                f"{field_name} mounted source requires 'path' or 'name'."
            )
        if path is not None and name is not None:
            raise InvalidSecretRefError(
                f"{field_name} mounted source accepts only one of 'path' or 'name'."
            )
        if name is not None:
            if "/" in name or name in {".", ".."}:
                raise InvalidSecretRefError(
                    f"{field_name} mounted secret name must be a plain basename."
                )
            path = str(Path("/run/secrets") / name)
            name = None
        else:
            if path is None:
                raise InvalidSecretRefError(
                    f"{field_name} mounted source requires 'path' or 'name'."
                )
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                path = str(Path("/run/secrets") / candidate)
            else:
                path = str(candidate)

    return SecretRef(source=source, path=path, name=name)


def secret_ref_from_file_path(path: Path | str) -> SecretRef:
    """Build a file SecretRef from a legacy plaintext path string."""
    return SecretRef(source=SecretSource.FILE, path=str(Path(path).expanduser()))


def secret_ref_roundtrip(ref: SecretRef) -> SecretRef:
    """Serialize then re-parse (tests / export safety)."""
    return parse_secret_ref(ref.to_mapping())


def mapping_without_secrets(data: dict[str, Any]) -> dict[str, Any]:
    """Shallow helper: replace SecretValue instances with redacted markers."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if hasattr(value, "get_secret_value") and type(value).__name__ == "SecretValue":
            out[key] = "***"
        elif isinstance(value, SecretRef):
            out[key] = value.to_mapping()
        else:
            out[key] = value
    return out


def asdict_safe(obj: object) -> dict[str, Any]:
    """dataclasses.asdict wrapper that redacts SecretValue if present."""
    from backuplint.secrets.value import SecretValue

    def _dict_factory(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if isinstance(value, SecretValue):
                result[key] = "***"
            elif isinstance(value, SecretRef):
                result[key] = value.to_mapping()
            else:
                result[key] = value
        return result

    return asdict(obj, dict_factory=_dict_factory)  # type: ignore[arg-type]
