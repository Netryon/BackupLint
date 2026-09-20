#!/usr/bin/env bash
# Accelerated v0.4 scheduler reliability lab (many short cycles).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="${TMPDIR:-/tmp}/backuplint-v04-accel-$$"
mkdir -p "$WORK/data" "$WORK/state"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

printf 'x\n' >"$WORK/data/file.txt"
cat >"$WORK/compose.yml" <<EOF
services:
  app:
    image: alpine:3.20
    command: ["sleep", "infinity"]
    volumes:
      - ./data:/data
EOF
cat >"$WORK/backuplint.yml" <<EOF
backup_paths:
  - $WORK/data
schedule:
  state_dir: $WORK/state
  history_limit: 200
  coverage:
    every: 1s
  integrity:
    enabled: false
    every: 1h
  restore_verification:
    enabled: false
    every: 1h
  deep_integrity:
    enabled: false
    every: 1h
EOF

cd "$ROOT"
backuplint daemon "$WORK/compose.yml" --config "$WORK/backuplint.yml" --poll-seconds 0.25 &
PID=$!
cleanup_daemon() {
  kill -TERM "$PID" 2>/dev/null || true
  wait "$PID" 2>/dev/null || true
  cleanup
}
trap cleanup_daemon EXIT

# Run for ~45s of accelerated cycles.
sleep 45
kill -TERM "$PID"
wait "$PID"

python3 - <<PY
import sqlite3, sys
db = "$WORK/state/history.sqlite3"
con = sqlite3.connect(db)
n = con.execute("select count(*) from runs").fetchone()[0]
results = [r[0] for r in con.execute("select result from runs")]
print(f"runs={n} results={results[-10:]}")
if n < 10:
    raise SystemExit(f"expected >=10 accelerated runs, got {n}")
if any(r not in {"PASS", "FAIL", "ERROR"} for r in results):
    raise SystemExit("unexpected result label")
# Coverage-only lab should PASS
if results.count("ERROR") > n // 2:
    raise SystemExit("too many ERROR results")
print("V04_ACCEL_OK")
PY
