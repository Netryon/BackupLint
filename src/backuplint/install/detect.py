"""Non-destructive external dependency detection for install planning."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from enum import StrEnum

from backuplint.process import TimeoutExpired, run_argv


class DependencyPresence(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DependencyCheck:
    name: str
    presence: DependencyPresence
    version: str | None
    check: str
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "presence": self.presence.value,
            "version": self.version,
            "check": self.check,
            "detail": self.detail,
        }


_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?)")


def _which_check(binary: str) -> DependencyCheck:
    path = shutil.which(binary)
    if path is None:
        return DependencyCheck(
            name=binary,
            presence=DependencyPresence.ABSENT,
            version=None,
            check=f"shutil.which({binary!r})",
            detail=f"{binary} not found on PATH",
        )
    return DependencyCheck(
        name=binary,
        presence=DependencyPresence.PRESENT,
        version=None,
        check=f"shutil.which({binary!r})",
        detail=f"found at {path}",
    )


def _version_from_argv(binary: str, argv: list[str], *, timeout: float = 5.0) -> DependencyCheck:
    base = _which_check(binary)
    if base.presence is DependencyPresence.ABSENT:
        return base
    resolved = shutil.which(binary)
    if resolved is None:
        return base
    try:
        result = run_argv([resolved, *argv], timeout=timeout)
    except TimeoutExpired:
        return DependencyCheck(
            name=binary,
            presence=DependencyPresence.PRESENT,
            version=None,
            check=" ".join([binary, *argv]),
            detail=f"{binary} present but version probe timed out",
        )
    except OSError as exc:
        return DependencyCheck(
            name=binary,
            presence=DependencyPresence.UNKNOWN,
            version=None,
            check=" ".join([binary, *argv]),
            detail=f"unable to execute {binary}: {exc}",
        )
    blob = (result.stdout or result.stderr or "").strip()
    match = _VERSION_RE.search(blob)
    version = match.group(1) if match else None
    if result.returncode != 0 and version is None:
        return DependencyCheck(
            name=binary,
            presence=DependencyPresence.PRESENT,
            version=None,
            check=" ".join([binary, *argv]),
            detail=f"{binary} present; version probe exit {result.returncode}",
        )
    return DependencyCheck(
        name=binary,
        presence=DependencyPresence.PRESENT,
        version=version,
        check=" ".join([binary, *argv]),
        detail=f"found; version={version or 'unparsed'}",
    )


def detect_restic() -> DependencyCheck:
    return _version_from_argv("restic", ["version"])


def detect_docker() -> DependencyCheck:
    # Prefer version over info (info can be expensive / need daemon).
    return _version_from_argv("docker", ["version", "--format", "{{.Client.Version}}"])


def detect_openssl() -> DependencyCheck:
    return _version_from_argv("openssl", ["version"])


def detect_python_module(module: str) -> DependencyCheck:
    """Check whether a BackupLint/project module is importable (installed support)."""
    check = f"import {module}"
    try:
        __import__(module)
    except ImportError as exc:
        return DependencyCheck(
            name=module,
            presence=DependencyPresence.ABSENT,
            version=None,
            check=check,
            detail=f"import failed: {exc}",
        )
    return DependencyCheck(
        name=module,
        presence=DependencyPresence.PRESENT,
        version=None,
        check=check,
        detail="importable",
    )


def detect_external_dependencies(
    *,
    names: tuple[str, ...] = ("restic", "docker", "openssl"),
) -> dict[str, DependencyCheck]:
    """Run safe, non-destructive probes. Never installs packages."""
    detectors = {
        "restic": detect_restic,
        "docker": detect_docker,
        "openssl": detect_openssl,
    }
    out: dict[str, DependencyCheck] = {}
    for name in names:
        detector = detectors.get(name)
        if detector is None:
            out[name] = DependencyCheck(
                name=name,
                presence=DependencyPresence.UNKNOWN,
                version=None,
                check="unknown",
                detail=f"no detector registered for {name!r}",
            )
        else:
            out[name] = detector()
    return out
