"""Optional fleet agent reporting configuration (v0.5)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backuplint.errors import ConfigError


@dataclass(frozen=True)
class FleetConfig:
    """Local agent settings for submitting scheduled results to a controller."""

    controller_url: str
    identity_dir: Path


def parse_fleet(raw: object, *, config_dir: Path | None) -> FleetConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'fleet' must be a mapping.")
    allowed = {"controller_url", "identity_dir"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(
            "Unknown fleet key(s): " + ", ".join(repr(key) for key in unknown)
        )
    url = raw.get("controller_url")
    if not isinstance(url, str) or not url.strip():
        raise ConfigError("'fleet.controller_url' must be a non-empty HTTPS URL.")
    url = url.strip()
    if not url.lower().startswith("https://"):
        raise ConfigError("'fleet.controller_url' must use https://")
    ident = raw.get("identity_dir")
    if not isinstance(ident, str) or not ident.strip():
        raise ConfigError("'fleet.identity_dir' must be a non-empty string path.")
    candidate = Path(ident.strip()).expanduser()
    if not candidate.is_absolute() and config_dir is not None:
        identity_dir = (config_dir / candidate).resolve()
    else:
        identity_dir = candidate
    return FleetConfig(controller_url=url, identity_dir=identity_dir)
