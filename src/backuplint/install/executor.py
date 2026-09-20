"""Native install execution: dry-run, mutate, rollback of installer-owned creates."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backuplint.install.deployment import DeploymentForm
from backuplint.install.detect import (
    DependencyPresence,
    detect_external_dependencies,
)
from backuplint.install.distro import DistroInfo, detect_distro
from backuplint.install.features import FeatureId, feature_definition
from backuplint.install.layout import (
    LayoutError,
    NativeLayout,
    apply_ownership,
    atomic_write_text,
    create_directories,
    default_config_stub,
    planned_paths,
    require_service_account,
    resolve_layout,
)
from backuplint.install.manifest import (
    InstallManifest,
    build_manifest,
    load_manifest,
    merge_features,
    write_manifest,
)
from backuplint.install.packages import (
    PackageAction,
    PackageManagerError,
    build_install_actions,
    execute_package_actions,
    packages_for_features,
    require_root,
)
from backuplint.install.profile import InstallProfile, ProfileError
from backuplint.install.roles import InstallationRole
from backuplint.install.support import ImplementationStatus
from backuplint.install.systemd_units import (
    SystemdUnit,
    generate_units,
    plan_systemd_commands,
    systemd_available,
)
from backuplint.process import CommandResult, run_argv


class InstallerError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class NativeInstallPlan:
    """Full mutation plan for native installer (serializable)."""

    role: InstallationRole
    deployment: DeploymentForm
    features: tuple[FeatureId, ...]
    distro: DistroInfo
    layout: NativeLayout
    package_actions: tuple[PackageAction, ...]
    directories: tuple[dict[str, object], ...]
    files_to_write: tuple[dict[str, object], ...]
    systemd_units: tuple[SystemdUnit, ...]
    systemd_commands: tuple[tuple[str, ...], ...]
    privilege_required: bool
    unsupported: tuple[str, ...]
    python_notes: tuple[str, ...]
    commands: tuple[tuple[str, ...], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "deployment": self.deployment.value,
            "features": [f.value for f in self.features],
            "distro": {
                "family": self.distro.family.value,
                "id": self.distro.id,
                "version_id": self.distro.version_id,
                "pretty_name": self.distro.pretty_name,
                "detail": self.distro.detail,
            },
            "layout": self.layout.to_dict(),
            "package_actions": [a.to_dict() for a in self.package_actions],
            "directories": list(self.directories),
            "files_to_write": list(self.files_to_write),
            "systemd_units": [u.to_dict() for u in self.systemd_units],
            "systemd_commands": [list(c) for c in self.systemd_commands],
            "privilege_required": self.privilege_required,
            "unsupported": list(self.unsupported),
            "python_notes": list(self.python_notes),
            "commands_that_would_execute": [list(c) for c in self.commands],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True) + "\n"


@dataclass
class InstallResult:
    ok: bool
    dry_run: bool
    plan: NativeInstallPlan
    created_paths: list[str] = field(default_factory=list)
    rolled_back: list[str] = field(default_factory=list)
    message: str = ""
    manifest: InstallManifest | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "message": self.message,
            "created_paths": self.created_paths,
            "rolled_back": self.rolled_back,
            "manifest": self.manifest.to_dict() if self.manifest else None,
            "plan": self.plan.to_dict(),
        }


def build_native_install_plan(
    profile: InstallProfile,
    *,
    prefix: Path | None = None,
    distro: DistroInfo | None = None,
    skip_present_packages: bool = True,
    backuplint_bin: str = "/usr/bin/backuplint",
) -> NativeInstallPlan:
    if profile.deployment is not DeploymentForm.NATIVE:
        raise InstallerError(
            "native installer only supports deployment: native "
            f"(got {profile.deployment.value})"
        )
    if profile.support.implementation is ImplementationStatus.UNSUPPORTED:
        raise InstallerError(profile.support.detail)

    info = distro or detect_distro()
    if not info.supported:
        raise InstallerError(f"unsupported distro for native install: {info.detail}")

    layout = resolve_layout(prefix=prefix)
    features = profile.enabled_features()
    pkgs = list(packages_for_features(features, family=info.family))

    if skip_present_packages:
        checks = detect_external_dependencies()
        keep: list[str] = []
        binary_for_pkg = {
            "docker.io": "docker",
            "docker": "docker",
            "restic": "restic",
            "openssl": "openssl",
        }
        for pkg in pkgs:
            binary = binary_for_pkg.get(pkg, pkg)
            check = checks.get(binary)
            if check is not None and check.presence is DependencyPresence.PRESENT:
                continue
            keep.append(pkg)
        pkgs = keep

    package_actions = build_install_actions(pkgs, distro=info)
    managed = planned_paths(layout, role=profile.role, features=features)
    directories = tuple(p.to_dict() for p in managed if p.kind == "directory")

    files: list[dict[str, object]] = []
    stub = default_config_stub(
        role=profile.role, schedule_state=layout.schedule_state
    )
    if any(p.kind == "file" and p.path == layout.config_file for p in managed):
        files.append(
            {
                "path": str(layout.config_file),
                "mode": "0o640",
                "purpose": "config stub",
                "bytes": len(stub.encode("utf-8")),
            }
        )

    units = generate_units(
        role=profile.role,
        features=features,
        layout=layout,
        backuplint_bin=backuplint_bin,
    )
    for unit in units:
        files.append(
            {
                "path": str(layout.systemd_dir / unit.filename),
                "mode": "0o644",
                "purpose": f"systemd unit ({unit.reason})",
                "bytes": len(unit.content.encode("utf-8")),
            }
        )
    files.append(
        {
            "path": str(layout.manifest_path),
            "mode": "0o640",
            "purpose": "installation manifest",
            "bytes": 0,
        }
    )

    systemd_cmds = plan_systemd_commands(units) if units else ()
    pkg_cmds = tuple(a.argv for a in package_actions)
    all_cmds = pkg_cmds + systemd_cmds

    unsupported: list[str] = []
    if profile.support.implementation is ImplementationStatus.LIMITED:
        unsupported.append(profile.support.detail)
    if profile.support.implementation is ImplementationStatus.PLANNED:
        unsupported.append(profile.support.detail)
    if units and not systemd_available():
        unsupported.append("systemd/systemctl not detected; units will be written only")

    python_notes = (
        "BackupLint Python package must already be installed "
        "(pip/editable/distro package); installer does not silently pip-install.",
        "Selected features require modules: "
        + ", ".join(
            sorted(
                {
                    m
                    for fid in features
                    for m in feature_definition(fid).python_modules
                }
            )
        ),
    )

    return NativeInstallPlan(
        role=profile.role,
        deployment=profile.deployment,
        features=features,
        distro=info,
        layout=layout,
        package_actions=package_actions,
        directories=directories,
        files_to_write=tuple(files),
        systemd_units=units,
        systemd_commands=systemd_cmds,
        privilege_required=True,
        unsupported=tuple(unsupported),
        python_notes=python_notes,
        commands=all_cmds,
    )


def _rollback(created: list[Path]) -> list[str]:
    rolled: list[str] = []
    for path in reversed(created):
        try:
            if path.is_symlink():
                continue
            if path.is_file():
                path.unlink()
                rolled.append(str(path))
            elif path.is_dir():
                # Only remove empty dirs created by us.
                try:
                    path.rmdir()
                    rolled.append(str(path))
                except OSError:
                    pass
        except OSError:
            continue
    return rolled


def execute_native_install(
    profile: InstallProfile,
    *,
    dry_run: bool = False,
    prefix: Path | None = None,
    assume_yes: bool = False,
    distro: DistroInfo | None = None,
    runner: Callable[..., CommandResult] | None = None,
    require_privileges: bool = True,
    geteuid: Callable[[], int] | None = None,
    start_services: bool = False,
    skip_present_packages: bool = True,
) -> InstallResult:
    """Execute or dry-run a native install. Never deletes user data on failure."""
    _ = assume_yes  # confirmation handled by CLI
    plan = build_native_install_plan(
        profile,
        prefix=prefix,
        distro=distro,
        skip_present_packages=skip_present_packages,
    )
    created: list[Path] = []

    if dry_run:
        return InstallResult(
            ok=True,
            dry_run=True,
            plan=plan,
            message="dry-run only; no host mutations performed",
        )

    if require_privileges and not require_root(geteuid=geteuid):
        raise InstallerError("native install requires root privileges")

    try:
        # Stage: packages
        execute_package_actions(
            plan.package_actions, runner=runner, dry_run=False
        )

        # Stage: directories
        managed = planned_paths(
            plan.layout, role=plan.role, features=plan.features
        )
        created.extend(create_directories(managed, dry_run=False))

        # System installs must have the service account before units are enabled.
        # Prefixed lab installs skip host account/ownership requirements.
        if prefix is None and plan.systemd_units:
            try:
                uid, gid = require_service_account()
            except LayoutError as exc:
                raise InstallerError(exc.message) from exc
            apply_ownership(managed, uid=uid, gid=gid, dry_run=False)

        # Stage: config stub (do not overwrite existing user config)
        config_path = plan.layout.config_file
        if not config_path.exists():
            stub = default_config_stub(
                role=plan.role, schedule_state=plan.layout.schedule_state
            )
            atomic_write_text(config_path, stub, mode=0o640, dry_run=False)
            created.append(config_path)

        # Stage: systemd units
        plan.layout.systemd_dir.mkdir(parents=True, exist_ok=True)
        for unit in plan.systemd_units:
            unit_path = plan.layout.systemd_dir / unit.filename
            existed = unit_path.exists()
            atomic_write_text(unit_path, unit.content, mode=0o644, dry_run=False)
            if not existed:
                created.append(unit_path)

        run = runner or (lambda argv, timeout=60.0: run_argv(list(argv), timeout=timeout))
        # Prefixed lab installs write unit files only; never touch host systemd.
        use_systemd = prefix is None and bool(plan.systemd_units) and systemd_available()
        if use_systemd:
            for cmd in plan.systemd_commands:
                completed = run(list(cmd), timeout=120.0)
                if completed.returncode != 0:
                    raise InstallerError(
                        f"systemd command failed: {' '.join(cmd)} "
                        f"(exit {completed.returncode})"
                    )
            if start_services:
                for unit in plan.systemd_units:
                    completed = run(
                        ["systemctl", "start", unit.filename], timeout=120.0
                    )
                    if completed.returncode != 0:
                        raise InstallerError(
                            f"failed to start {unit.filename}"
                        )

        previous = None
        if plan.layout.manifest_path.exists():
            try:
                previous = load_manifest(plan.layout.manifest_path)
            except Exception:  # noqa: BLE001
                previous = None

        manifest = build_manifest(
            role=plan.role,
            deployment=plan.deployment,
            installed_features=plan.features,
            managed_paths=tuple(str(p["path"]) for p in plan.directories)
            + tuple(str(f["path"]) for f in plan.files_to_write),
            managed_units=tuple(u.filename for u in plan.systemd_units),
            dependency_notes=tuple(
                f"{a.manager.value}: {' '.join(a.packages)}"
                for a in plan.package_actions
                if a.packages
            ),
            previous=previous,
        )
        write_manifest(plan.layout.manifest_path, manifest, dry_run=False)
        if not previous:
            created.append(plan.layout.manifest_path)

        return InstallResult(
            ok=True,
            dry_run=False,
            plan=plan,
            created_paths=[str(p) for p in created],
            message="native install completed",
            manifest=manifest,
        )
    except (InstallerError, PackageManagerError, LayoutError, ProfileError, OSError) as exc:
        rolled = _rollback(created)
        message = getattr(exc, "message", str(exc))
        return InstallResult(
            ok=False,
            dry_run=False,
            plan=plan,
            created_paths=[str(p) for p in created],
            rolled_back=rolled,
            message=message,
        )


def add_feature(
    feature: FeatureId,
    *,
    dry_run: bool = False,
    prefix: Path | None = None,
    distro: DistroInfo | None = None,
    runner: Callable[..., CommandResult] | None = None,
    geteuid: Callable[[], int] | None = None,
) -> InstallResult:
    """Install support for one additional feature; does not enable runtime config."""
    layout = resolve_layout(prefix=prefix)
    if not layout.manifest_path.exists():
        raise InstallerError(
            "no installation manifest found; run full install before install-feature"
        )
    manifest = load_manifest(layout.manifest_path)
    if feature in manifest.installed_features:
        # Idempotent: already installed.
        profile = _profile_from_manifest(manifest)
        plan = build_native_install_plan(profile, prefix=prefix, distro=distro)
        return InstallResult(
            ok=True,
            dry_run=dry_run,
            plan=plan,
            message=f"feature {feature.value} already installed (not enabled)",
            manifest=manifest,
        )

    # Validate role compatibility via a synthetic profile.
    from backuplint.install.features import FEATURE_REGISTRY
    from backuplint.install.profile import normalize_and_validate

    features_map = {fid.value: (fid in manifest.installed_features) for fid in FEATURE_REGISTRY}
    features_map[feature.value] = True
    # Ensure role-required flags stay on.
    if manifest.role is InstallationRole.AGENT:
        features_map["fleet_agent"] = True
    if manifest.role in {InstallationRole.CONTROLLER, InstallationRole.ALL_IN_ONE}:
        features_map["fleet_controller"] = True
    profile = normalize_and_validate(
        {
            "schema_version": 1,
            "role": manifest.role.value,
            "deployment": manifest.deployment.value,
            "features": features_map,
        }
    )
    result = execute_native_install(
        profile,
        dry_run=dry_run,
        prefix=prefix,
        distro=distro,
        runner=runner,
        geteuid=geteuid,
        assume_yes=True,
        require_privileges=True,
    )
    if result.ok and result.manifest is not None and not dry_run:
        # Ensure merged feature list includes the new feature.
        merged = merge_features(manifest.installed_features, (feature,))
        updated = build_manifest(
            role=manifest.role,
            deployment=manifest.deployment,
            installed_features=merged,
            managed_paths=result.manifest.managed_paths,
            managed_units=result.manifest.managed_units,
            dependency_notes=result.manifest.dependency_notes,
            previous=manifest,
        )
        write_manifest(layout.manifest_path, updated, dry_run=False)
        result.manifest = updated
        result.message = (
            f"feature {feature.value} installed; runtime remains disabled until "
            "configured (installed != enabled)"
        )
    return result


def _profile_from_manifest(manifest: InstallManifest) -> InstallProfile:
    from backuplint.install.features import FEATURE_REGISTRY
    from backuplint.install.profile import normalize_and_validate

    features = {
        fid.value: (fid in manifest.installed_features) for fid in FEATURE_REGISTRY
    }
    return normalize_and_validate(
        {
            "schema_version": 1,
            "role": manifest.role.value,
            "deployment": manifest.deployment.value,
            "features": features,
        }
    )


def build_uninstall_plan(
    *,
    prefix: Path | None = None,
) -> dict[str, Any]:
    """Plan removal of installer-owned units/files only. Never purges secrets/data."""
    layout = resolve_layout(prefix=prefix)
    if not layout.manifest_path.exists():
        raise InstallerError("no installation manifest; nothing to uninstall")
    manifest = load_manifest(layout.manifest_path)
    remove_units = list(manifest.managed_units)
    # Only remove unit files under systemd_dir that we manage.
    unit_paths = [str(layout.systemd_dir / name) for name in remove_units]
    return {
        "will_remove": {
            "systemd_unit_files": unit_paths,
            "manifest": str(layout.manifest_path),
        },
        "will_disable_services": remove_units,
        "will_not_delete": [
            str(layout.controller_state),
            str(layout.agent_identity),
            str(layout.schedule_state),
            str(layout.config_file),
            str(layout.log),
            "restic repositories",
            "CA/private keys",
            "user data / evidence",
        ],
        "commands": [
            *([["systemctl", "disable", "--now", u] for u in remove_units]),
            ["systemctl", "daemon-reload"],
        ],
        "note": (
            "Uninstall removes installer-owned service files/manifest only. "
            "Use a separate explicit purge command to delete state (not implemented "
            "as default)."
        ),
    }


def execute_uninstall(
    *,
    prefix: Path | None = None,
    dry_run: bool = False,
    runner: Callable[..., CommandResult] | None = None,
) -> dict[str, Any]:
    plan = build_uninstall_plan(prefix=prefix)
    if dry_run:
        return {"ok": True, "dry_run": True, "plan": plan}
    layout = resolve_layout(prefix=prefix)
    run = runner or (lambda argv, timeout=60.0: run_argv(list(argv), timeout=timeout))
    for cmd in plan["commands"]:
        run(list(cmd), timeout=120.0)
    for unit_path in plan["will_remove"]["systemd_unit_files"]:
        path = Path(unit_path)
        if path.is_file() and not path.is_symlink():
            path.unlink()
    # Remove manifest last; keep state dirs.
    if layout.manifest_path.is_file():
        layout.manifest_path.unlink()
    return {"ok": True, "dry_run": False, "plan": plan}


def which_backuplint() -> str:
    return shutil.which("backuplint") or "/usr/bin/backuplint"
