#!/usr/bin/env bash
# Advanced-test planner (dry-run by default). Does not start endurance/distro/ARM labs.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PACK="$(cd "$(dirname "$0")" && pwd)"
MODE="${1:-plan}"
EVIDENCE_ROOT="${BACKUPLINT_AT_EVIDENCE_ROOT:-${TMPDIR:-/tmp}/backuplint-advanced-test}"
SHA="$(git -C "$ROOT" rev-parse HEAD)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
EVIDENCE="$EVIDENCE_ROOT/plan-$STAMP"

if [[ "$MODE" != "plan" && "$MODE" != "dry-run" ]]; then
  echo "usage: $0 [plan|dry-run]" >&2
  echo "Refusing execution mode. Advanced campaign must be started explicitly later." >&2
  exit 4
fi

mkdir -p "$EVIDENCE"
have() { command -v "$1" >/dev/null 2>&1; }

runnable=()
unavailable=()
deferred=(
  ">=72h mixed endurance (scripts/pre-v1-endurance-launcher.py)"
  "~7-day endurance"
  "destructive real-world restore corruption labs"
  "full distro matrix execution"
  "ARM/Pi campaign execution"
)

check() {
  local name="$1" ok="$2" detail="$3"
  if [[ "$ok" == yes ]]; then
    runnable+=("$name — $detail")
  else
    unavailable+=("$name — $detail")
  fi
}

ARCH="$(uname -m)"
check "native-controller-smoke" yes "scripts/advanced-test/run-native-controller.sh"
check "native-agent" yes "needs live controller URL/token/CA"
check "container-controller-build" "$(have docker && echo yes || echo no)" "docker required"
check "container-agent" "$(have docker && echo yes || echo no)" "scripts/advanced-test/run-container-agent.sh (Dockerfile.agent)"
check "mixed-topology" "$(have docker && echo yes || echo no)" "docker + native agent and/or container agent"
check "restic-lab" "$(have restic && echo yes || echo no)" "restic on PATH"
check "siem-receiver" yes "scripts/advanced-test/run-siem-receiver.sh"
check "policy-rollout-doc-path" yes "scripts/advanced-test/run-policy-rollout.sh"
check "upgrade-recovery-exercise" yes "scripts/advanced-test/run-upgrade-recovery.sh"
check "distro-debian/fedora/rocky" no "set BACKUPLINT_GUEST to real guest"
if [[ "$ARCH" == aarch64 || "$ARCH" == arm64 || "$ARCH" == armv7l ]]; then
  check "arm-pi" yes "host arch $ARCH"
else
  check "arm-pi" no "host arch $ARCH (need real ARM/Pi)"
fi

ORDER=(
  "1. prerequisite inventory (this plan)"
  "2. native controller + agent smoke"
  "3. container controller build + mixed topology"
  "4. restic lab + isolated restore (non-destructive first)"
  "5. external SIEM receiver proof"
  "6. policy rollout/rollback/drift proof"
  "7. upgrade + controller recovery"
  "8. distro guests (Debian/Fedora/Rocky)"
  "9. ARM/Pi"
  "10. >=72h endurance (explicit launch only)"
)

{
  echo "BackupLint advanced-test plan"
  echo "sha=$SHA"
  echo "mode=$MODE"
  echo "evidence=$EVIDENCE"
  echo
  echo "## Intended order"
  printf '%s\n' "${ORDER[@]}"
  echo
  echo "## Runnable on this machine now"
  printf -- '- %s\n' "${runnable[@]}"
  echo
  echo "## Unavailable / blocked here"
  printf -- '- %s\n' "${unavailable[@]}"
  echo
  echo "## Explicitly deferred (do not auto-start)"
  printf -- '- %s\n' "${deferred[@]}"
  echo
  echo "## Human result summaries"
  echo "Collect completed scenario blocks using:"
  echo "  $ROOT/docs/internal/pre-v1-reality-result-template.md"
  echo "Copy filled blocks into: $EVIDENCE/results/"
} | tee "$EVIDENCE/plan.txt"

mkdir -p "$EVIDENCE/results"
cp "$ROOT/docs/internal/pre-v1-reality-result-template.md" "$EVIDENCE/results/TEMPLATE.md"
echo "$SHA" >"$EVIDENCE/sha.txt"
echo "PLAN_OK evidence=$EVIDENCE"
