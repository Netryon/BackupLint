#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
EVIDENCE="${BACKUPLINT_AT_EVIDENCE:-${TMPDIR:-/tmp}/backuplint-advanced-test/mixed}"
mkdir -p "$EVIDENCE"
command -v docker >/dev/null || { echo "PREREQ_MISSING: docker" >&2; exit 3; }
echo "Mixed topology plan:"
echo "  1) build/start container controller"
echo "  2) enroll native agent with controller CA/token"
echo "  3) heartbeat + submit + policy poll"
echo "Record results with docs/internal/pre-v1-reality-result-template.md"
echo "PACK_STEP_READY evidence=$EVIDENCE sha=$(git -C "$ROOT" rev-parse HEAD)"
