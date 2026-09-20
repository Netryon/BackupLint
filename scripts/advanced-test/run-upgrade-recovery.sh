#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
EVIDENCE="${BACKUPLINT_AT_EVIDENCE:-${TMPDIR:-/tmp}/backuplint-advanced-test/upgrade-recovery}"
mkdir -p "$EVIDENCE"
export PYTHONPATH="$ROOT:${PYTHONPATH:-}"
export BACKUPLINT_SIEM_TOKEN="${BACKUPLINT_SIEM_TOKEN:-advanced-test-siem}"
python3 "$ROOT/scripts/pre-v1-upgrade-recovery-exercise.py" \
  --work-dir "$EVIDENCE/work" \
  --report "$EVIDENCE/report.json"
echo "UPGRADE_RECOVERY_EXERCISE_DONE report=$EVIDENCE/report.json"
