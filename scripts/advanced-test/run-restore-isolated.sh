#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
EVIDENCE="${BACKUPLINT_AT_EVIDENCE:-${TMPDIR:-/tmp}/backuplint-advanced-test/restore}"
REPO="${1:-}"
mkdir -p "$EVIDENCE"
command -v restic >/dev/null || { echo "PREREQ_MISSING: restic" >&2; exit 3; }
[[ -n "$REPO" ]] || { echo "usage: $0 <restic_repo_path>" >&2; exit 3; }
[[ -d "$REPO" ]] || { echo "PREREQ_MISSING: repo $REPO" >&2; exit 3; }
echo "Isolated restore verification should use backuplint restore path into $EVIDENCE/dest"
echo "Never restore over live production data."
echo "PACK_STEP_READY sha=$(git -C "$ROOT" rev-parse HEAD)"
echo "Wire to: python -m backuplint ... restore verification against disposable dest"
