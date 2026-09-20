#!/usr/bin/env bash
# Platform matrix runner stubs for deferred advanced real-world testing.
# Do NOT claim PASS for platforms not actually executed.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EVIDENCE_ROOT="${1:-${TMPDIR:-/tmp}/backuplint-platform-matrix}"
mkdir -p "$EVIDENCE_ROOT"

cat <<EOF
BackupLint pre-v1 platform matrix commands
Repo: $ROOT
Evidence root: $EVIDENCE_ROOT

Currently validated in this workshop host only:
  - Ubuntu x86_64 (this machine)

Deferred (run on real guests; do not fake):
EOF

for distro in debian fedora rocky; do
  cat <<EOF

## $distro (x86_64)
# Prepare guest with Python 3.11+ and git, then:
ssh <guest> 'bash -s' <<'REMOTE'
set -euo pipefail
sudo apt-get update && sudo apt-get install -y python3 python3-venv git  # Debian/Ubuntu
# Fedora/Rocky: sudo dnf install -y python3 python3-pip git
git clone <repo-url> backuplint && cd backuplint
git checkout feat/pre-v1-hardening
python3 -m venv .venv && . .venv/bin/activate
pip install -U pip && pip install -e '.[dev]'
ruff check src tests
pytest -q tests/unit
pytest -q tests/integration/test_controller_container.py tests/integration/test_siem_controller_export.py
python scripts/guest-validate.sh
REMOTE
# Capture: uname -a, /etc/os-release, pytest summaries → $EVIDENCE_ROOT/$distro/
EOF
done

cat <<'EOF'

## Raspberry Pi / ARM
# On aarch64 host with supported Python:
uname -m   # must show aarch64/armv7l
cd backuplint && . .venv/bin/activate
pytest -q tests/unit
python scripts/dashboard-platform-smoke.py
# Capture evidence under scale-evidence/pre-v1-platform-matrix/arm/

## Native vs container
# Native: scripts/guest-validate.sh
# Container: pytest -q tests/integration/test_controller_container.py
# Mixed: controller in container + agent native (document URLs/certs)

Status: SCRIPTS_READY — execution deferred to advanced real-world phase.
EOF
