#!/usr/bin/env bash
# Real Docker lab: PostgreSQL persistent storage + database warning.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$LAB"
mkdir -p pgdata

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
# Protected bind + database warning => WARN, exit 0
if [[ "$EC" -ne 0 ]]; then
  echo "Expected exit 0 (WARN) for covered DB bind + warning, got $EC" >&2
  exit 1
fi
echo "$OUT" | grep -qE "Result: WARN" || { echo "missing WARN" >&2; exit 1; }
echo "$OUT" | grep -qE "Database workload detected" || { echo "missing DB warning" >&2; exit 1; }
echo "$OUT" | grep -qE "protected" || { echo "missing protected mount" >&2; exit 1; }
echo "==> database-container lab passed"
