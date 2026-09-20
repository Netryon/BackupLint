#!/usr/bin/env bash
set -euo pipefail
EVIDENCE="${BACKUPLINT_AT_EVIDENCE:-${TMPDIR:-/tmp}/backuplint-advanced-test/restic}"
mkdir -p "$EVIDENCE/repo" "$EVIDENCE/data" "$EVIDENCE/restore"
command -v restic >/dev/null || { echo "PREREQ_MISSING: restic" >&2; exit 3; }
export RESTIC_PASSWORD="${RESTIC_PASSWORD:-advanced-test-restic-pass}"
export RESTIC_REPOSITORY="$EVIDENCE/repo"
if [[ ! -f "$RESTIC_REPOSITORY/config" ]]; then
  restic init
fi
echo "seed-$(date -u +%Y%m%dT%H%M%SZ)" >"$EVIDENCE/data/file.txt"
restic backup "$EVIDENCE/data"
restic check
echo "RESTIC_LAB_OK repo=$RESTIC_REPOSITORY"
echo "Next: intentional corruption + backuplint integrity/restore (run-restore-isolated.sh)"
