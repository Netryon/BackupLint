#!/usr/bin/env bash
# Scenario D: parent backup path covers multiple app dirs → PASS
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$LAB"
mkdir -p apps/sonarr apps/radarr

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
if [[ "$EC" -ne 0 ]]; then
  echo "Expected exit 0 for parent-path coverage, got $EC" >&2
  exit 1
fi
echo "$OUT" | grep -qE "Result: PASS" || { echo "missing PASS" >&2; exit 1; }
echo "==> nested-paths lab passed"
