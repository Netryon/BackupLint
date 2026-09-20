"""Constrained package-manager adapters (apt/dnf). Argv lists only."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from backuplint.install.detect import DependencyPresence
from backuplint.install.distro import DistroFamily, DistroInfo
from backuplint.install.features import FeatureId
from backuplint.process import CommandResult, run_argv


class PackageManagerError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PackageManagerKind(StrEnum):
    APT = "apt"
    DNF = "dnf"
    NONE = "none"


Runner = Callable[..., CommandResult]


@dataclass(frozen=True, slots=True)
class PackageAction:
    """One planned package-manager invocation (never a shell string)."""

    manager: PackageManagerKind
    argv: tuple[str, ...]
    reason: str
    packages: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "manager": self.manager.value,
            "argv": list(self.argv),
            "reason": self.reason,
            "packages": list(self.packages),
        }


# Explicit feature → distro package mapping (no profile-supplied package names).
_APT_PACKAGES: dict[str, str] = {
    "docker": "docker.io",
    "restic": "restic",
    "openssl": "openssl",
}
_DNF_PACKAGES: dict[str, str] = {
    "docker": "docker",
    "restic": "restic",
    "openssl": "openssl",
}


def packages_for_features(
    features: Sequence[FeatureId],
    *,
    family: DistroFamily,
) -> tuple[str, ...]:
    """Map enabled features to distro package names."""
    from backuplint.install.features import feature_definition

    needed_binaries: list[str] = []
    for fid in features:
        needed_binaries.extend(feature_definition(fid).external_binaries)
    mapping = _APT_PACKAGES if family is DistroFamily.DEBIAN else _DNF_PACKAGES
    packages: list[str] = []
    for binary in dict.fromkeys(needed_binaries):
        pkg = mapping.get(binary)
        if pkg is not None:
            packages.append(pkg)
    return tuple(dict.fromkeys(packages))


def build_install_actions(
    packages: Sequence[str],
    *,
    distro: DistroInfo,
) -> tuple[PackageAction, ...]:
    if not packages:
        return ()
    if distro.family is DistroFamily.DEBIAN:
        return (
            PackageAction(
                manager=PackageManagerKind.APT,
                argv=("apt-get", "update"),
                reason="refresh apt indexes before installing BackupLint dependencies",
                packages=(),
            ),
            PackageAction(
                manager=PackageManagerKind.APT,
                argv=(
                    "apt-get",
                    "install",
                    "-y",
                    "--no-install-recommends",
                    *packages,
                ),
                reason="install required external packages for selected features",
                packages=tuple(packages),
            ),
        )
    if distro.family is DistroFamily.RHEL:
        return (
            PackageAction(
                manager=PackageManagerKind.DNF,
                argv=("dnf", "install", "-y", *packages),
                reason="install required external packages for selected features",
                packages=tuple(packages),
            ),
        )
    raise PackageManagerError(distro.detail)


def require_root(*, geteuid: Callable[[], int] | None = None) -> bool:
    checker = geteuid or os.geteuid
    return checker() == 0


def execute_package_actions(
    actions: Sequence[PackageAction],
    *,
    runner: Runner | None = None,
    dry_run: bool = False,
) -> list[dict[str, object]]:
    """Run planned argv actions. Dry-run records without executing."""
    run = runner or (lambda argv, timeout=120.0: run_argv(list(argv), timeout=timeout))
    results: list[dict[str, object]] = []
    for action in actions:
        entry: dict[str, object] = {
            **action.to_dict(),
            "dry_run": dry_run,
            "status": "planned" if dry_run else "pending",
        }
        if dry_run:
            entry["status"] = "dry_run"
            results.append(entry)
            continue
        completed = run(list(action.argv), timeout=600.0)
        entry["returncode"] = completed.returncode
        entry["stdout_tail"] = (completed.stdout or "")[-500:]
        entry["stderr_tail"] = (completed.stderr or "")[-500:]
        if completed.returncode != 0:
            entry["status"] = "failed"
            results.append(entry)
            raise PackageManagerError(
                f"package command failed ({completed.returncode}): "
                f"{' '.join(action.argv)}"
            )
        entry["status"] = "ok"
        results.append(entry)
    return results


def presence_to_skip(
    binary: str, presence: DependencyPresence
) -> bool:
    return presence is DependencyPresence.PRESENT
