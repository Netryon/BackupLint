#!/usr/bin/env bash
# Disposable prefix lab for native installer (no root, no host systemd).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LAB="${NATIVE_INSTALL_LAB:-/tmp/bl-native-installer-lab-$$}"
PROFILE="$LAB/profile.yaml"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
rm -rf "$LAB"
mkdir -p "$LAB"
cat > "$PROFILE" <<'YAML'
schema_version: 1
role: all_in_one
deployment: native
features:
  coverage: true
  integrity: false
  restore_verification: false
  scheduler: true
  fleet_controller: true
YAML

python3 - <<PY
from pathlib import Path
from backuplint.install.distro import DistroFamily, DistroInfo
from backuplint.install.executor import (
    add_feature,
    build_uninstall_plan,
    execute_native_install,
    execute_uninstall,
)
from backuplint.install.features import FeatureId
from backuplint.install.profile import load_profile_file

lab = Path("$LAB")
profile = load_profile_file(lab / "profile.yaml")
distro = DistroInfo(DistroFamily.DEBIAN, "debian", "12", "Debian", "lab")

def runner(argv, timeout=60.0):
    from backuplint.process import CommandResult
    return CommandResult(0, "ok", "")

r1 = execute_native_install(
    profile, prefix=lab / "root", distro=distro, runner=runner,
    require_privileges=False, skip_present_packages=True,
)
assert r1.ok and not r1.dry_run, r1.message
dry = execute_native_install(
    profile, prefix=lab / "root", distro=distro, runner=runner,
    require_privileges=False, dry_run=True,
)
assert dry.ok and dry.dry_run
r2 = execute_native_install(
    profile, prefix=lab / "root", distro=distro, runner=runner,
    require_privileges=False,
)
assert r2.ok, r2.message
added = add_feature(
    FeatureId.INTEGRITY, prefix=lab / "root", distro=distro, runner=runner,
    geteuid=lambda: 0,
)
assert added.ok, added.message
plan = build_uninstall_plan(prefix=lab / "root")
assert "will_not_delete" in plan
print("NATIVE_INSTALL_PREFIX_LAB_OK", lab)
PY
