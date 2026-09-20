#!/usr/bin/env bash
# Scenario C: named volume intentionally omitted from backup_paths → FAIL
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$LAB"
mkdir -p not-the-volume-dir

cleanup() {
  sg docker -c "docker compose -f compose.yml down -v --remove-orphans" >/dev/null 2>&1 || true
}
trap cleanup EXIT

sg docker -c "docker compose -f compose.yml up -d --quiet-pull" >/dev/null

set +e
OUT="$(sg docker -c "cd '$LAB' && '$ROOT/.venv/bin/backuplint' scan compose.yml --config backuplint.yml" 2>&1)"
EC=$?
set -e

echo "$OUT"
if [[ "$EC" -ne 1 ]]; then
  echo "Expected exit 1 for missing named volume coverage, got $EC" >&2
  exit 1
fi
echo "$OUT" | grep -qE "Result: FAIL" || { echo "missing FAIL" >&2; exit 1; }
echo "$OUT" | grep -qE "not protected|critical" || { echo "missing critical finding" >&2; exit 1; }
echo "==> missing-volume lab passed"
