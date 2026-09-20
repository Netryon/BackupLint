"""Native installer unit tests (mocked package manager / systemd / prefix FS)."""

from __future__ import annotations

from pathlib import Path

import pytest

from backuplint.install.deployment import DeploymentForm
from backuplint.install.distro import DistroFamily, DistroInfo, detect_distro
from backuplint.install.executor import (
    InstallerError,
    add_feature,
    build_native_install_plan,
    build_uninstall_plan,
    execute_native_install,
    execute_uninstall,
)
from backuplint.install.features import FeatureId
from backuplint.install.layout import (
    LayoutError,
    atomic_write_text,
    create_directories,
    planned_paths,
    resolve_layout,
)
from backuplint.install.manifest import (
    ManifestError,
    build_manifest,
    load_manifest,
    parse_manifest,
    write_manifest,
)
from backuplint.install.packages import (
    build_install_actions,
    packages_for_features,
)
from backuplint.install.profile import normalize_and_validate
from backuplint.install.roles import InstallationRole
from backuplint.install.systemd_units import generate_units
from backuplint.install.wizard import WizardCancelled, run_interactive_wizard
from backuplint.process import CommandResult


def _debian() -> DistroInfo:
    return DistroInfo(
        family=DistroFamily.DEBIAN,
        id="debian",
        version_id="12",
        pretty_name="Debian 12",
        detail="apt",
    )


def _rhel() -> DistroInfo:
    return DistroInfo(
        family=DistroFamily.RHEL,
        id="rocky",
        version_id="9",
        pretty_name="Rocky 9",
        detail="dnf",
    )


def _profile(role: str, **features: bool):
    raw: dict[str, object] = {
        "schema_version": 1,
        "role": role,
        "deployment": "native",
    }
    if features:
        raw["features"] = features
    return normalize_and_validate(raw)


def _ok_runner(argv: list[str], timeout: float = 60.0) -> CommandResult:
    _ = timeout
    return CommandResult(returncode=0, stdout="ok", stderr="")


@pytest.mark.parametrize(
    "role",
    ["standalone", "agent", "controller", "all_in_one"],
)
def test_plans_for_all_roles(tmp_path: Path, role: str) -> None:
    profile = _profile(role)
    plan = build_native_install_plan(
        profile, prefix=tmp_path, distro=_debian(), skip_present_packages=False
    )
    assert plan.role.value == role
    assert plan.deployment is DeploymentForm.NATIVE
    assert plan.privilege_required is True
    data = plan.to_dict()
    assert "commands_that_would_execute" in data
    assert "directories" in data


def test_apt_and_dnf_adapters() -> None:
    features = (FeatureId.COVERAGE, FeatureId.INTEGRITY, FeatureId.FLEET_CONTROLLER)
    apt_pkgs = packages_for_features(features, family=DistroFamily.DEBIAN)
    dnf_pkgs = packages_for_features(features, family=DistroFamily.RHEL)
    assert "docker.io" in apt_pkgs
    assert "restic" in apt_pkgs
    assert "openssl" in apt_pkgs
    assert "docker" in dnf_pkgs
    apt_actions = build_install_actions(apt_pkgs, distro=_debian())
    assert apt_actions[0].argv[0] == "apt-get"
    assert "shell" not in " ".join(apt_actions[0].argv)
    dnf_actions = build_install_actions(dnf_pkgs, distro=_rhel())
    assert dnf_actions[0].argv[0] == "dnf"


def test_unsupported_distro(tmp_path: Path) -> None:
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=alpine\nPRETTY_NAME="Alpine"\n', encoding="utf-8")
    info = detect_distro(os_release_path=os_release)
    assert info.family is DistroFamily.UNSUPPORTED
    profile = _profile("controller")
    with pytest.raises(InstallerError, match="unsupported distro"):
        build_native_install_plan(profile, prefix=tmp_path, distro=info)


def test_dry_run_does_not_mutate(tmp_path: Path) -> None:
    profile = _profile("standalone")
    before = {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
    result = execute_native_install(
        profile,
        dry_run=True,
        prefix=tmp_path,
        distro=_debian(),
        runner=_ok_runner,
        require_privileges=False,
    )
    assert result.ok and result.dry_run
    after = {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
    assert before == after


def test_execute_install_idempotent(tmp_path: Path) -> None:
    profile = _profile("controller")
    r1 = execute_native_install(
        profile,
        prefix=tmp_path,
        distro=_debian(),
        runner=_ok_runner,
        require_privileges=False,
    )
    assert r1.ok
    layout = resolve_layout(prefix=tmp_path)
    assert layout.manifest_path.is_file()
    assert (layout.systemd_dir / "backuplint-controller.service").is_file()
    r2 = execute_native_install(
        profile,
        prefix=tmp_path,
        distro=_debian(),
        runner=_ok_runner,
        require_privileges=False,
    )
    assert r2.ok
    manifest = load_manifest(layout.manifest_path)
    assert FeatureId.FLEET_CONTROLLER in manifest.installed_features


def test_package_failure_rolls_back_created_files(tmp_path: Path) -> None:
    profile = _profile(
        "standalone",
        coverage=True,
        integrity=True,
        scheduler=True,
        restore_verification=False,
    )

    def fail_runner(argv: list[str], timeout: float = 60.0) -> CommandResult:
        _ = timeout
        if argv and argv[0] in {"apt-get", "dnf"}:
            return CommandResult(1, "", "boom")
        return CommandResult(0, "", "")

    result = execute_native_install(
        profile,
        prefix=tmp_path,
        distro=_debian(),
        runner=fail_runner,
        require_privileges=False,
        skip_present_packages=False,
    )
    assert result.ok is False
    assert "package command failed" in result.message
    assert resolve_layout(prefix=tmp_path).manifest_path.exists() is False


def test_directory_failure_rollback(tmp_path: Path) -> None:
    layout = resolve_layout(prefix=tmp_path)
    blocker = layout.state
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("not-a-dir", encoding="utf-8")
    managed = planned_paths(
        layout,
        role=InstallationRole.STANDALONE,
        features=(FeatureId.COVERAGE, FeatureId.SCHEDULER),
    )
    with pytest.raises(LayoutError):
        create_directories(managed, dry_run=False)


def test_container_profile_rejected_by_native_installer(tmp_path: Path) -> None:
    profile = normalize_and_validate(
        {"schema_version": 1, "role": "agent", "deployment": "container"}
    )
    with pytest.raises(InstallerError, match="native"):
        build_native_install_plan(profile, prefix=tmp_path, distro=_debian())


def test_symlink_refusal(tmp_path: Path) -> None:
    layout = resolve_layout(prefix=tmp_path)
    layout.etc.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "target"
    target.mkdir()
    layout.etc.symlink_to(target)
    managed = planned_paths(
        layout,
        role=InstallationRole.CONTROLLER,
        features=(FeatureId.FLEET_CONTROLLER,),
    )
    # existing symlink for etc should be refused when chmod/ensure runs
    with pytest.raises(LayoutError, match="symlink"):
        create_directories(managed, dry_run=False)


def test_atomic_write_and_manifest_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    manifest = build_manifest(
        role=InstallationRole.AGENT,
        deployment=DeploymentForm.NATIVE,
        installed_features=(FeatureId.COVERAGE, FeatureId.FLEET_AGENT),
        managed_paths=("/x",),
        managed_units=("backuplint-scheduler.service",),
    )
    write_manifest(path, manifest)
    loaded = load_manifest(path)
    assert loaded.role is InstallationRole.AGENT
    assert FeatureId.FLEET_AGENT in loaded.installed_features
    # Upgrade/re-read: rewrite with previous preserves created_at
    updated = build_manifest(
        role=loaded.role,
        deployment=loaded.deployment,
        installed_features=loaded.installed_features + (FeatureId.SCHEDULER,),
        managed_paths=loaded.managed_paths,
        managed_units=loaded.managed_units,
        previous=loaded,
    )
    write_manifest(path, updated)
    again = load_manifest(path)
    assert again.created_at == loaded.created_at
    assert FeatureId.SCHEDULER in again.installed_features


def test_manifest_rejects_secrets_and_bad_version() -> None:
    with pytest.raises(ManifestError, match="schema_version"):
        parse_manifest({"schema_version": 99, "role": "agent", "deployment": "native"})
    with pytest.raises(ManifestError, match="secret"):
        parse_manifest(
            {
                "schema_version": 1,
                "role": "agent",
                "deployment": "native",
                "token": "nope",
                "installed_features": [],
            }
        )


def test_systemd_units_no_duplicates_all_in_one(tmp_path: Path) -> None:
    layout = resolve_layout(prefix=tmp_path)
    units = generate_units(
        role=InstallationRole.ALL_IN_ONE,
        features=(
            FeatureId.COVERAGE,
            FeatureId.SCHEDULER,
            FeatureId.FLEET_CONTROLLER,
        ),
        layout=layout,
    )
    names = [u.filename for u in units]
    assert names.count("backuplint-controller.service") == 1
    assert names.count("backuplint-scheduler.service") == 1
    assert "backuplint-agent.service" not in names
    assert "Restart=on-failure" in units[0].content


def test_root_required(tmp_path: Path) -> None:
    profile = _profile("controller")
    with pytest.raises(InstallerError, match="root"):
        execute_native_install(
            profile,
            prefix=tmp_path,
            distro=_debian(),
            runner=_ok_runner,
            require_privileges=True,
            geteuid=lambda: 1000,
        )


def test_feature_add_preserves_installed_not_enabled(tmp_path: Path) -> None:
    profile = _profile(
        "standalone",
        coverage=True,
        integrity=False,
        restore_verification=False,
        scheduler=True,
    )
    result = execute_native_install(
        profile,
        prefix=tmp_path,
        distro=_debian(),
        runner=_ok_runner,
        require_privileges=False,
    )
    assert result.ok
    added = add_feature(
        FeatureId.INTEGRITY,
        prefix=tmp_path,
        distro=_debian(),
        runner=_ok_runner,
        geteuid=lambda: 0,
    )
    assert added.ok
    assert "installed != enabled" in added.message or "disabled" in added.message
    manifest = load_manifest(resolve_layout(prefix=tmp_path).manifest_path)
    assert FeatureId.INTEGRITY in manifest.installed_features


def test_uninstall_plan_preserves_secrets(tmp_path: Path) -> None:
    profile = _profile("controller")
    execute_native_install(
        profile,
        prefix=tmp_path,
        distro=_debian(),
        runner=_ok_runner,
        require_privileges=False,
    )
    layout = resolve_layout(prefix=tmp_path)
    # Simulate sensitive state present
    layout.controller_state.mkdir(parents=True, exist_ok=True)
    (layout.controller_state / "ca.key").write_text("SECRET", encoding="utf-8")
    plan = build_uninstall_plan(prefix=tmp_path)
    assert str(layout.controller_state) in plan["will_not_delete"]
    assert any("CA" in x or "private" in x for x in plan["will_not_delete"])
    # dry execute uninstall should remove unit/manifest but not ca.key
    execute_uninstall(prefix=tmp_path, dry_run=False, runner=_ok_runner)
    assert (layout.controller_state / "ca.key").read_text(encoding="utf-8") == "SECRET"
    assert not layout.manifest_path.exists()


def test_wizard_cancel_no_side_effects(tmp_path: Path) -> None:
    answers = iter(["q"])

    def prompt(_msg: str) -> str:
        return next(answers)

    with pytest.raises(WizardCancelled):
        run_interactive_wizard(prompt=prompt, echo=lambda _s: None)


def test_wizard_confirm_builds_profile() -> None:
    # standalone defaults: accept defaults then confirm yes
    answers = iter(
        [
            "standalone",  # role
            "",  # coverage default y
            "",  # integrity default y
            "",  # restore default n
            "",  # scheduler default y
            # fleet_* skipped for standalone
            "",  # siem_export default n
            "y",  # confirm
        ]
    )

    def prompt(_msg: str) -> str:
        return next(answers)

    result = run_interactive_wizard(prompt=prompt, echo=lambda _s: None)
    assert result.confirmed
    assert result.profile.role is InstallationRole.STANDALONE
    assert result.profile.features[FeatureId.FLEET_CONTROLLER] is False


def test_path_traversal_rejected_in_atomic_parent(tmp_path: Path) -> None:
    # Writing under prefix only — ensure symlink escape parent is refused when
    # final path is symlink.
    dest = tmp_path / "etc" / "backuplint" / "backuplint.yml"
    dest.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    dest.symlink_to(outside / "x")
    with pytest.raises(LayoutError, match="symlink"):
        atomic_write_text(dest, "data\n", mode=0o640)


def test_existing_config_not_overwritten(tmp_path: Path) -> None:
    profile = _profile("standalone")
    layout = resolve_layout(prefix=tmp_path)
    layout.etc.mkdir(parents=True)
    layout.config_file.write_text("backup_paths: [/custom]\n", encoding="utf-8")
    result = execute_native_install(
        profile,
        prefix=tmp_path,
        distro=_debian(),
        runner=_ok_runner,
        require_privileges=False,
    )
    assert result.ok
    assert layout.config_file.read_text(encoding="utf-8") == "backup_paths: [/custom]\n"
