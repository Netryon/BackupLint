"""Discover mounts from Docker Compose configuration."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from backuplint.classify import classify_mount
from backuplint.models import Mount, MountType, ServiceMounts
from backuplint.process import TimeoutExpired, run_argv


class ComposeError(Exception):
    """User-facing Compose discovery failure."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _mount_type(raw: str | None) -> MountType:
    if raw is None:
        return MountType.UNKNOWN
    try:
        return MountType(raw)
    except ValueError:
        return MountType.UNKNOWN


def parse_compose_config(config: dict) -> list[ServiceMounts]:
    """Parse mounts from a resolved Compose config object."""
    if "services" not in config or config.get("services") is None:
        services: dict = {}
    else:
        services = config["services"]
    if not isinstance(services, dict):
        raise ComposeError("Compose configuration has an invalid services section.")

    results: list[ServiceMounts] = []
    volume_names = _volume_name_map(config)
    for service_name, service in services.items():
        if not isinstance(service, dict):
            continue
        mounts: list[Mount] = []
        for entry in service.get("volumes") or []:
            if isinstance(entry, str):
                # Resolved `docker compose config --format json` uses objects.
                # Keep a conservative fallback for unexpected string forms.
                parts = entry.split(":")
                if len(parts) < 2:
                    continue
                source, target = parts[0], parts[1]
                read_only = len(parts) >= 3 and "ro" in parts[2].split(",")
                if "/" not in source and not source.startswith("."):
                    mtype = MountType.VOLUME
                    source = volume_names.get(source, source)
                else:
                    mtype = MountType.BIND
                mounts.append(
                    Mount(
                        service=str(service_name),
                        type=mtype,
                        source=source,
                        target=target,
                        read_only=read_only,
                    )
                )
                continue
            if not isinstance(entry, dict):
                continue
            source = entry.get("source") or ""
            target = entry.get("target") or ""
            if not target and entry.get("type") != "tmpfs":
                continue
            mtype = _mount_type(entry.get("type"))
            if mtype is MountType.VOLUME:
                source = volume_names.get(str(source), str(source))
            mounts.append(
                Mount(
                    service=str(service_name),
                    type=mtype,
                    source=str(source),
                    target=str(target),
                    read_only=bool(entry.get("read_only", False)),
                )
            )
        results.append(
            ServiceMounts(
                name=str(service_name),
                mounts=tuple(mounts),
                image=_service_image(service),
            )
        )

    results.sort(key=lambda item: item.name)
    return results


def _service_image(service: dict) -> str | None:
    image = service.get("image")
    if isinstance(image, str) and image.strip():
        return image.strip()
    return None


def _volume_name_map(config: dict) -> dict[str, str]:
    """Map Compose volume keys to concrete Docker volume names when provided."""
    mapping: dict[str, str] = {}
    raw = config.get("volumes") or {}
    if not isinstance(raw, dict):
        return mapping
    for key, spec in raw.items():
        if isinstance(spec, dict) and spec.get("name"):
            mapping[str(key)] = str(spec["name"])
        else:
            mapping[str(key)] = str(key)
    return mapping


def load_compose_config(compose_file: Path) -> dict:
    """Load resolved Compose JSON via `docker compose config`."""
    path = compose_file.expanduser()
    if not path.exists():
        raise ComposeError(f"Compose file not found: {path}")
    if not path.is_file():
        raise ComposeError(f"Compose path is not a file: {path}")

    docker = shutil.which("docker")
    if docker is None:
        raise ComposeError("Docker is not installed or not available on PATH.")

    # Use an argument list (never shell=True) and pin the project directory to
    # the Compose file location so relative bind mounts resolve predictably.
    command = [
        docker,
        "compose",
        "-f",
        str(path.resolve()),
        "config",
        "--format",
        "json",
    ]
    try:
        # Command is a fixed argv list: resolved `docker` binary plus constant
        # Compose subcommands. Compose file path is validated as an existing file.
        completed = run_argv(
            command,
            timeout=60,
            cwd=str(path.resolve().parent),
        )
    except TimeoutExpired as exc:
        raise ComposeError("Docker Compose configuration parsing timed out.") from exc
    except OSError as exc:
        raise ComposeError(f"Failed to run Docker Compose: {exc}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error").strip()
        raise ComposeError(f"Docker Compose failed to parse configuration:\n{detail}")

    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ComposeError("Docker Compose returned invalid JSON configuration.") from exc

    if not isinstance(data, dict):
        raise ComposeError("Docker Compose returned an unexpected configuration shape.")
    return data


def discover_mounts(compose_file: Path) -> list[ServiceMounts]:
    """Discover service mounts from a Compose file."""
    return parse_compose_config(load_compose_config(compose_file))


def format_mount_report(services: list[ServiceMounts]) -> str:
    """Render a human-readable mount discovery report with storage classes."""
    if not services:
        return "No services found."

    blocks: list[str] = []
    for service in services:
        lines = [service.name]
        if not service.mounts:
            lines.append("  (no mounts)")
        else:
            for mount in service.mounts:
                storage_class = classify_mount(mount)
                lines.append(f"{mount.summary_line()}  [{storage_class.value}]")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
