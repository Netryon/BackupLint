#!/usr/bin/env bash
# Deliberately omit vaultwarden from backup_paths and expect failure.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$LAB"

mkdir -p vaultwarden sonarr

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
if [[ "$EC" -ne 1 ]]; then
  echo "Expected exit 1 for missing bind coverage, got $EC" >&2
  exit 1
fi
echo "$OUT" | grep -qE "vaultwarden" || { echo "missing vaultwarden finding" >&2; exit 1; }
echo "$OUT" | grep -qE "Result: FAIL" || { echo "missing FAIL result" >&2; exit 1; }
echo "$OUT" | grep -qE "critical" || { echo "missing critical summary" >&2; exit 1; }
echo "==> missing-bind-mount lab passed"
