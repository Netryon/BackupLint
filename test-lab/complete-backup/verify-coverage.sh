#!/usr/bin/env bash
# Verify coverage PASS when lab bind mounts are included in backup_paths.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$LAB"

mkdir -p data config

cleanup() {
  sg docker -c "docker compose -f compose.yml down -v --remove-orphans" >/dev/null 2>&1 || true
}
trap cleanup EXIT

sg docker -c "docker compose -f compose.yml up -d --quiet-pull" >/dev/null

export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"
set +e
OUT="$(sg docker -c "cd '$LAB' && '$ROOT/.venv/bin/backuplint' scan compose.yml --config backuplint.yml" 2>&1)"
EC=$?
set -e

echo "$OUT"
if [[ "$EC" -ne 0 ]]; then
  echo "Expected exit 0 for complete coverage, got $EC" >&2
  exit 1
fi
echo "$OUT" | grep -qE "Result: PASS" || { echo "missing PASS result" >&2; exit 1; }
echo "$OUT" | grep -qE "BackupLint Audit" || { echo "missing audit header" >&2; exit 1; }
echo "==> complete-backup coverage lab passed"
