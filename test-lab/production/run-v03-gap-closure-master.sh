#!/usr/bin/env bash
# Master v0.3 gap-closure campaign orchestrator (workshop).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate" 2>/dev/null || true
cd "$ROOT"

unset RESTIC_PASSWORD RESTIC_PASSWORD_FILE RESTIC_REPOSITORY || true

LOGDIR="${GAP_LOGDIR:-/tmp/backuplint-v03-gap-logs}"
mkdir -p "$LOGDIR"

PASS=0
FAIL=0
ok() { echo "OK $*"; PASS=$((PASS + 1)); }
bad() { echo "FAIL $*"; FAIL=$((FAIL + 1)); }

run_step() {
  local name="$1"
  shift
  echo
  echo "########## $name ##########"
  set +e
  "$@" >"$LOGDIR/${name}.log" 2>&1
  local ec=$?
  set -e
  if [[ "$ec" -eq 0 ]]; then
    ok "$name"
  else
    bad "$name (exit=$ec) — see $LOGDIR/${name}.log"
    tail -40 "$LOGDIR/${name}.log" || true
  fi
  return 0
}

echo "Gap-closure master — logs under $LOGDIR"
echo "HEAD=$(git rev-parse --short HEAD)"
echo "VERSION=$(python -c 'import backuplint; import importlib.metadata as m; print(m.version(\"backuplint\"))' 2>/dev/null || echo 0.3.0.dev0)"

run_step unit pytest -q tests/unit
run_step integration env -u RESTIC_PASSWORD -u RESTIC_PASSWORD_FILE -u RESTIC_REPOSITORY \
  pytest -q tests/integration
run_step integrity bash test-lab/production/run-v02-integrity-campaign.sh
run_step restore bash test-lab/production/run-v03-restore-campaign.sh
run_step remote_backends bash test-lab/production/run-v03-gap-remote-backends.sh
run_step network_faults bash test-lab/production/run-v03-gap-network-faults.sh
run_step soak bash test-lab/production/run-v03-gap-soak.sh
run_step app_recovery bash test-lab/production/run-v03-app-recovery.sh
run_step ruff ruff check src tests
run_step bandit bandit -q -r src/backuplint
run_step pip_audit pip-audit

echo
echo "=== GAP MASTER summary PASS=$PASS FAIL=$FAIL ==="
if [[ "$FAIL" -eq 0 ]]; then
  echo GAP_CLOSURE_CAMPAIGN_PASSED
  exit 0
fi
echo GAP_CLOSURE_CAMPAIGN_FAILED
exit 1
