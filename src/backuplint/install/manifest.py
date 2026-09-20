"""Durable installation manifest (no secrets)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backuplint import __version__
from backuplint.install.deployment import DeploymentForm
from backuplint.install.features import FeatureId
from backuplint.install.layout import atomic_write_text
from backuplint.install.roles import InstallationRole

MANIFEST_SCHEMA_VERSION = 1


class ManifestError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class InstallManifest:
    schema_version: int
    role: InstallationRole
    deployment: DeploymentForm
    installed_features: tuple[FeatureId, ...]
    installer_version: str
    managed_paths: tuple[str, ...]
    managed_units: tuple[str, ...]
    dependency_notes: tuple[str, ...]
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "role": self.role.value,
            "deployment": self.deployment.value,
            "installed_features": [f.value for f in self.installed_features],
            "installer_version": self.installer_version,
            "managed_paths": list(self.managed_paths),
            "managed_units": list(self.managed_units),
            "dependency_notes": list(self.dependency_notes),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def build_manifest(
    *,
    role: InstallationRole,
    deployment: DeploymentForm,
    installed_features: tuple[FeatureId, ...],
    managed_paths: tuple[str, ...],
    managed_units: tuple[str, ...],
    dependency_notes: tuple[str, ...] = (),
    previous: InstallManifest | None = None,
    installer_version: str | None = None,
) -> InstallManifest:
    created = previous.created_at if previous is not None else _now()
    return InstallManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        role=role,
        deployment=deployment,
        installed_features=tuple(sorted(installed_features, key=lambda f: f.value)),
        installer_version=installer_version or __version__,
        managed_paths=tuple(sorted(set(managed_paths))),
        managed_units=tuple(sorted(set(managed_units))),
        dependency_notes=dependency_notes,
        created_at=created,
        updated_at=_now(),
    )


def write_manifest(
    path: Path, manifest: InstallManifest, *, dry_run: bool = False
) -> None:
    atomic_write_text(path, manifest.to_json(), mode=0o640, dry_run=dry_run)


def load_manifest(path: Path) -> InstallManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"unable to read install manifest: {exc}") from exc
    return parse_manifest(raw)


def parse_manifest(raw: object) -> InstallManifest:
    if not isinstance(raw, dict):
        raise ManifestError("manifest must be a JSON object")
    version = raw.get("schema_version")
    if version != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(
            f"unsupported manifest schema_version {version!r}; "
            f"supported={MANIFEST_SCHEMA_VERSION}"
        )
    try:
        role = InstallationRole(str(raw["role"]))
        deployment = DeploymentForm(str(raw["deployment"]))
        features = tuple(FeatureId(str(x)) for x in raw.get("installed_features", []))
    except (KeyError, ValueError) as exc:
        raise ManifestError(f"invalid manifest fields: {exc}") from exc
    # Reject secret-looking keys if ever introduced.
    forbidden = {"password", "token", "private_key", "client_key", "secret"}
    if forbidden & {str(k).lower() for k in raw}:
        raise ManifestError("manifest must not contain secret fields")
    return InstallManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        role=role,
        deployment=deployment,
        installed_features=features,
        installer_version=str(raw.get("installer_version") or ""),
        managed_paths=tuple(str(x) for x in raw.get("managed_paths", [])),
        managed_units=tuple(str(x) for x in raw.get("managed_units", [])),
        dependency_notes=tuple(str(x) for x in raw.get("dependency_notes", [])),
        created_at=str(raw.get("created_at") or ""),
        updated_at=str(raw.get("updated_at") or ""),
    )


def merge_features(
    existing: tuple[FeatureId, ...], added: tuple[FeatureId, ...]
) -> tuple[FeatureId, ...]:
    return tuple(sorted(set(existing) | set(added), key=lambda f: f.value))
