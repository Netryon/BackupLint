#!/usr/bin/env bash
# Advanced-test pack — orchestrator for later real-world campaign.
# Does not execute the full campaign by default; validates prerequisites and
# prints the exact next commands.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PACK="$(cd "$(dirname "$0")" && pwd)"
EVIDENCE="${1:-${TMPDIR:-/tmp}/backuplint-advanced-test}"
mkdir -p "$EVIDENCE"

fail_need() {
  echo "PREREQ_MISSING: $*" >&2
  exit 3
}

need_cmd() { command -v "$1" >/dev/null || fail_need "command '$1'"; }

echo "BackupLint advanced-test pack"
echo "repo=$ROOT"
echo "evidence=$EVIDENCE"
echo
echo "Checking local workshop prereqs (not platform guests)..."
need_cmd python3
need_cmd git
need_cmd openssl
if ! command -v restic >/dev/null; then
  echo "WARN: restic not on PATH — real Restic scenarios will BLOCKED until installed"
fi
if ! command -v docker >/dev/null; then
  echo "WARN: docker not on PATH — container scenarios will BLOCKED"
fi

cat <<EOF

Prepared runners (execute on the appropriate machine; do not fake):
  $PACK/run-native-controller.sh
  $PACK/run-native-agent.sh
  $PACK/run-container-controller.sh
  $PACK/run-container-agent.sh
  $PACK/run-mixed-topology.sh
  $PACK/run-restic-lab.sh
  $PACK/run-restore-isolated.sh
  $PACK/run-siem-receiver.sh
  $PACK/run-policy-rollout.sh
  $PACK/run-upgrade-recovery.sh
  $PACK/run-distro-guest.sh <debian|fedora|rocky>
  $PACK/run-arm-pi.sh

Result template:
  $ROOT/docs/internal/pre-v1-reality-result-template.md

Gate checklist:
  $ROOT/docs/internal/pre-v1-release-gates.md

Status: PACK_READY — campaign execution deferred.
EOF
