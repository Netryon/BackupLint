#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
EVIDENCE="${BACKUPLINT_AT_EVIDENCE:-${TMPDIR:-/tmp}/backuplint-advanced-test/policy}"
mkdir -p "$EVIDENCE"
echo "Policy rollout/rollback/drift proof uses controller store APIs + agent poll_and_apply_policy."
echo "Prefer scripts/pre-v1-endurance-harness.py fault phases as a workload reference,"
echo "then record human results via docs/internal/pre-v1-reality-result-template.md"
echo "PACK_STEP_READY sha=$(git -C "$ROOT" rev-parse HEAD) evidence=$EVIDENCE"
