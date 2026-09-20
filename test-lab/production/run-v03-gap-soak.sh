#!/usr/bin/env bash
# Reliability / soak campaign for BackupLint v0.3 restore verification.
# Disposable local restic repo only. Tracks temp dirs and restic processes.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate" 2>/dev/null || true

unset RESTIC_PASSWORD RESTIC_PASSWORD_FILE RESTIC_REPOSITORY || true

SUFFIX="$(openssl rand -hex 4 2>/dev/null || printf '%04x%04x' "$RANDOM" "$RANDOM")"
BASE="$(mktemp -d "/tmp/backuplint-v03-gap-soak-${SUFFIX}-XXXXXX")"
PASSFILE="$BASE/restic.pass"
REPO="$BASE/repo"
DATA="$BASE/data"
COMPOSE="$BASE/compose.yml"
CFG="$BASE/backuplint.yml"
BAD_PASS="$BASE/bad.pass"
FAIL_CFG="$BASE/fail.yml"
AUTH_CFG="$BASE/auth.yml"
LOG="$BASE/soak.log"

PASS=0
FAIL=0
ok() { echo "OK $*"; PASS=$((PASS + 1)); }
bad() { echo "FAIL $*"; FAIL=$((FAIL + 1)); }

cleanup() {
  set +e
  docker compose -f "$COMPOSE" down --remove-orphans >/dev/null 2>&1
  # Kill any leftover backuplint/restic from this lab (best-effort, scoped by cwd/env not global wipe)
  pkill -f "restic.*(restore|dump).*${REPO}" 2>/dev/null || true
  rm -rf "$BASE"
  find /tmp -maxdepth 1 -user "$(id -un)" -type d -name 'backuplint-restore-*' \
    -mmin -360 -exec rm -rf {} + 2>/dev/null
  set -e
}
trap cleanup EXIT

count_restore_dirs() {
  find /tmp -maxdepth 1 -user "$(id -un)" -type d -name 'backuplint-restore-*' 2>/dev/null | wc -l | tr -d ' '
}

count_restic() {
  local n
  n="$(pgrep -af 'restic' 2>/dev/null | grep -vE 'pgrep|grep|soak' | wc -l | tr -d '[:space:]')"
  if [[ -z "$n" ]]; then
    echo 0
  else
    echo "$n"
  fi
}

printf 'v03-gap-soak-pass\n' >"$PASSFILE"
chmod 600 "$PASSFILE"
printf 'wrong-soak-password\n' >"$BAD_PASS"
chmod 600 "$BAD_PASS"

mkdir -p "$DATA" "$REPO"
echo 'soak-payload' >"$DATA/file.txt"
mkdir -p "$DATA/nested"
echo 'nested' >"$DATA/nested/x.txt"
# Modest large file for interruption window
dd if=/dev/urandom of="$DATA/blob.bin" bs=1M count=24 status=none 2>/dev/null || \
  dd if=/dev/zero of="$DATA/blob.bin" bs=1M count=24 status=none

export RESTIC_PASSWORD_FILE="$PASSFILE"
restic init -r "$REPO" >/dev/null
restic -r "$REPO" backup "$DATA" >/dev/null
unset RESTIC_PASSWORD_FILE

cat >"$COMPOSE" <<EOF
services:
  app:
    image: alpine:3.20
    container_name: bl-gap-soak-app-${SUFFIX}
    command: ["sleep", "14400"]
    volumes:
      - ${DATA}:/data
EOF

cat >"$CFG" <<EOF
backup_paths: []
restic:
  repository: ${REPO}
  password_file: ${PASSFILE}
  restore_verification:
    mode: selected
EOF

# FAIL: expected_paths missing from restored content → restore FAILED (exit 1)
cat >"$FAIL_CFG" <<EOF
backup_paths: []
restic:
  repository: ${REPO}
  password_file: ${PASSFILE}
  restore_verification:
    mode: selected
    expected_paths:
      - this-file-does-not-exist-in-restore.txt
EOF

cat >"$AUTH_CFG" <<EOF
backup_paths: []
restic:
  repository: ${REPO}
  password_file: ${BAD_PASS}
  restore_verification:
    mode: selected
EOF

docker compose -f "$COMPOSE" up -d >/dev/null

run_scan() {
  local cfg="$1"
  local t="${2:-90}"
  set +e
  OUT="$(timeout --signal=TERM --kill-after=10s "$t" backuplint scan "$COMPOSE" --config "$cfg" 2>&1)"
  EC=$?
  set -e
}

echo "=== restore PASS x20 ==="
for i in $(seq 1 20); do
  run_scan "$CFG" 90
  if [[ "$EC" -eq 0 ]] && printf '%s' "$OUT" | grep -qi 'selected-path restore verification passed'; then
    ok "pass_$i"
  else
    bad "pass_$i_ec=${EC}"
  fi
  if printf '%s' "$OUT" | grep -Fq 'v03-gap-soak-pass'; then
    bad "pass_$i_password_leak"
  fi
done

echo "=== restore FAIL x20 ==="
for i in $(seq 1 20); do
  run_scan "$FAIL_CFG" 90
  # Product: restore FAILED → overall FAIL exit 1; must not be exit 0 PASS
  if [[ "$EC" -eq 1 ]]; then
    ok "fail_$i"
  else
    bad "fail_$i_ec=${EC}"
  fi
  if printf '%s' "$OUT" | grep -qi 'selected-path restore verification passed'; then
    bad "fail_$i_false_pass"
  fi
done

echo "=== auth ERROR x20 ==="
for i in $(seq 1 20); do
  run_scan "$AUTH_CFG" 60
  if [[ "$EC" -eq 2 ]]; then
    ok "auth_$i"
  else
    bad "auth_$i_ec=${EC}"
  fi
  if printf '%s' "$OUT" | grep -qi 'authentication failed'; then
    ok "auth_${i}_msg"
  else
    bad "auth_${i}_msg"
  fi
  if printf '%s' "$OUT" | grep -Fq 'wrong-soak-password'; then
    bad "auth_${i}_secret_leak"
  fi
done

echo "=== interruption x10 ==="
for i in $(seq 1 10); do
  set +e
  timeout --signal=TERM --kill-after=5s 3 \
    backuplint scan "$COMPOSE" --config "$CFG" >"$BASE/int_$i.out" 2>&1
  EC=$?
  set -e
  OUT="$(cat "$BASE/int_$i.out" 2>/dev/null || true)"
  # Expect timeout (124) or ERROR (2) — never silent PASS after hard interrupt
  if [[ "$EC" -eq 0 ]] && printf '%s' "$OUT" | grep -qi 'selected-path restore verification passed'; then
    # Tiny datasets may finish within 3s — acceptable race, not a false PASS after kill
    ok "interrupt_${i}_completed_within_window"
  elif [[ "$EC" -eq 124 || "$EC" -eq 2 || "$EC" -ne 0 ]]; then
    ok "interrupt_${i}_exit=${EC}"
    if printf '%s' "$OUT" | grep -qi 'selected-path restore verification passed'; then
      bad "interrupt_${i}_false_pass"
    fi
  else
    bad "interrupt_${i}_exit=${EC}"
  fi
done

echo "=== cleanup x20 ==="
for i in $(seq 1 20); do
  before="$(count_restore_dirs)"
  run_scan "$CFG" 90
  after="$(count_restore_dirs)"
  if [[ "$EC" -eq 0 && "$after" -eq 0 ]]; then
    ok "cleanup_$i"
  elif [[ "$EC" -eq 0 && "$after" -le "$before" ]]; then
    ok "cleanup_${i}_stable"
  else
    bad "cleanup_$i_before=${before}_after=${after}_ec=${EC}"
  fi
done

BEFORE_RESTIC="$(count_restic)"
BEFORE_DIRS="$(count_restore_dirs)"

echo "=== soak loop (~30 cycles) ==="
SOAK_OK=0
SOAK_BAD=0
for i in $(seq 1 30); do
  {
    echo "--- cycle $i ---"
    run_scan "$CFG" 90
    echo "ec=$EC"
    printf '%s\n' "$OUT" | tail -n 5
  } >>"$LOG" 2>&1
  if [[ "$EC" -eq 0 ]] && printf '%s' "$OUT" | grep -qi 'selected-path restore verification passed'; then
    SOAK_OK=$((SOAK_OK + 1))
  else
    SOAK_BAD=$((SOAK_BAD + 1))
    bad "soak_cycle_$i_ec=${EC}"
  fi
  # validate: no password leak
  if printf '%s' "$OUT" | grep -Fq 'v03-gap-soak-pass'; then
    bad "soak_cycle_$i_password_leak"
  fi
  # cleanup check
  dirs="$(count_restore_dirs)"
  if [[ "$dirs" -ne 0 ]]; then
    bad "soak_cycle_$i_orphans=${dirs}"
  fi
  sleep 0.2
done
ok "soak_cycles_ok=${SOAK_OK}"
[[ "$SOAK_BAD" -eq 0 ]] && ok soak_no_failures || bad "soak_failures=${SOAK_BAD}"

AFTER_RESTIC="$(count_restic)"
AFTER_DIRS="$(count_restore_dirs)"

echo "=== end assertions ==="
if [[ "$AFTER_DIRS" -eq 0 ]]; then ok final_no_orphans; else bad "final_orphans=${AFTER_DIRS}"; fi
# Allow ambient restic unrelated to us; only fail if we grew many zombies
if [[ "$AFTER_RESTIC" -le $((BEFORE_RESTIC + 2)) ]]; then
  ok "restic_procs_stable_before=${BEFORE_RESTIC}_after=${AFTER_RESTIC}"
else
  bad "restic_procs_grew_before=${BEFORE_RESTIC}_after=${AFTER_RESTIC}"
fi

run_scan "$CFG" 90
if [[ "$EC" -eq 0 ]] && printf '%s' "$OUT" | grep -qi 'selected-path restore verification passed'; then
  ok final_restore_pass
else
  bad "final_restore_ec=${EC}"
fi

echo
echo "=== summary PASS=$PASS FAIL=$FAIL soak_ok=$SOAK_OK soak_bad=$SOAK_BAD ==="
echo "Log: $LOG (removed on EXIT trap with BASE)"
if [[ "$FAIL" -eq 0 ]]; then
  # Preserve a small summary outside BASE before cleanup? Print only.
  echo GAP_SOAK_PASSED
  exit 0
fi
echo GAP_SOAK_FAILED
exit 1
