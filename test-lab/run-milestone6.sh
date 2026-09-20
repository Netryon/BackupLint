#!/usr/bin/env bash
# Run Milestone 6 realistic backup coverage scenarios A–E.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

labs=(
  "test-lab/complete-backup/verify-coverage.sh"
  "test-lab/missing-bind-mount/verify-coverage.sh"
  "test-lab/missing-volume/verify-coverage.sh"
  "test-lab/nested-paths/verify-coverage.sh"
  "test-lab/edge-cases/verify-coverage.sh"
)

for lab in "${labs[@]}"; do
  echo "======== Running $lab ========"
  bash "$lab"
  echo
done

echo "All Milestone 6 coverage labs passed."
