#!/usr/bin/env bash
# Scenario E: ./app must not cover ./app2 → FAIL
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$LAB"
mkdir -p app app2

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
  echo "Expected exit 1 for similar-path false parent, got $EC" >&2
  exit 1
fi
echo "$OUT" | grep -qE "Result: FAIL" || { echo "missing FAIL" >&2; exit 1; }
echo "==> edge-cases similar-path lab passed"
