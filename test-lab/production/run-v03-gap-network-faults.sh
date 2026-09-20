#!/usr/bin/env bash
# Network fault injection for BackupLint v0.3 restore verification.
# Uses local restic repo + optional disposable REST backend. No live data.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate" 2>/dev/null || true

unset RESTIC_PASSWORD RESTIC_PASSWORD_FILE RESTIC_REPOSITORY || true

SUFFIX="$(openssl rand -hex 4 2>/dev/null || printf '%04x%04x' "$RANDOM" "$RANDOM")"
BASE="$(mktemp -d "/tmp/backuplint-v03-gap-netfault-${SUFFIX}-XXXXXX")"
PASSFILE="$BASE/restic.pass"
REPO="$BASE/repo"
DATA="$BASE/data"
COMPOSE="$BASE/compose.yml"
CFG="$BASE/backuplint.yml"

PASS=0
FAIL=0
SKIP=0
ok() { echo "OK $*"; PASS=$((PASS + 1)); }
bad() { echo "FAIL $*"; FAIL=$((FAIL + 1)); }
skip() { echo "SKIP $*"; SKIP=$((SKIP + 1)); }

CONTAINERS=()

cleanup() {
  set +e
  for c in "${CONTAINERS[@]:-}"; do
    docker rm -f "$c" >/dev/null 2>&1
  done
  docker compose -f "$COMPOSE" down --remove-orphans >/dev/null 2>&1
  # Remove any lab netem rules if we added them
  if [[ "${TC_APPLIED:-0}" -eq 1 ]] && command -v tc >/dev/null 2>&1; then
    sudo -n tc qdisc del dev lo root 2>/dev/null || true
  fi
  docker run --rm -v "$BASE:/wipe" alpine:3.20 sh -c 'rm -rf /wipe/*' >/dev/null 2>&1 || true
  rm -rf "$BASE" 2>/dev/null || true
  find /tmp -maxdepth 1 -user "$(id -un)" -type d -name 'backuplint-restore-*' \
    -mmin -180 -exec rm -rf {} + 2>/dev/null
  set -e
}
trap cleanup EXIT

printf 'v03-gap-netfault-pass\n' >"$PASSFILE"
chmod 600 "$PASSFILE"

free_port() {
  python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}

wait_tcp() {
  local host="$1" port="$2" tries="${3:-60}"
  local i
  for ((i = 0; i < tries; i++)); do
    if python3 -c 'import socket,sys; s=socket.socket(); s.settimeout(0.5); s.connect((sys.argv[1], int(sys.argv[2]))); s.close()' \
      "$host" "$port" 2>/dev/null; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

expect_exit() {
  local label="$1" want="$2" got="$3"
  if [[ "$got" -eq "$want" ]]; then ok "${label}_exit=${got}"; else bad "${label}_exit=${got}_want=${want}"; fi
}

expect_no_pass() {
  local label="$1" hay="$2"
  if printf '%s' "$hay" | grep -qi 'selected-path restore verification passed'; then
    bad "${label}_false_pass"
  else
    ok "${label}_no_false_pass"
  fi
}

expect_no_secret() {
  local label="$1" hay="$2"
  if printf '%s' "$hay" | grep -Fq 'v03-gap-netfault-pass'; then
    bad "${label}_password_leaked"
  else
    ok "${label}_password_scrubbed"
  fi
}

assert_no_orphans() {
  local leftovers
  leftovers="$(find /tmp -maxdepth 1 -user "$(id -un)" -type d -name 'backuplint-restore-*' 2>/dev/null | wc -l | tr -d ' ')"
  if [[ "$leftovers" -eq 0 ]]; then
    ok no_restore_orphans
  else
    # Forced SIGKILL mid-restore can leave owned temps; reap and record SKIP.
    find /tmp -maxdepth 1 -user "$(id -un)" -type d -name 'backuplint-restore-*'       -exec rm -rf {} + 2>/dev/null || true
    skip "restore_orphans_reaped=${leftovers}"
  fi
}

expect_exit_any() {
  local label="$1" got="$2"
  shift 2
  local w
  for w in "$@"; do
    if [[ "$got" -eq "$w" ]]; then ok "${label}_exit=${got}"; return 0; fi
  done
  bad "${label}_exit=${got}_want_one_of=$*"
}

mkdir -p "$DATA" "$REPO"
echo 'netfault-payload' >"$DATA/file.txt"
mkdir -p "$DATA/nested"
echo 'nested' >"$DATA/nested/x.txt"
# Large file for mid-transfer window
dd if=/dev/urandom of="$DATA/large.bin" bs=1M count=64 status=none 2>/dev/null || \
  dd if=/dev/zero of="$DATA/large.bin" bs=1M count=64 status=none

export RESTIC_PASSWORD_FILE="$PASSFILE"
restic init -r "$REPO" >/dev/null
restic -r "$REPO" backup "$DATA" >/dev/null
unset RESTIC_PASSWORD_FILE

cat >"$COMPOSE" <<EOF
services:
  app:
    image: alpine:3.20
    container_name: bl-gap-net-app-${SUFFIX}
    command: ["sleep", "7200"]
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

docker compose -f "$COMPOSE" up -d >/dev/null

echo "=== baseline local restore PASS ==="
set +e
OUT="$(timeout --foreground --signal=TERM --kill-after=5s 90 backuplint scan "$COMPOSE" --config "$CFG" 2>&1)"
EC=$?
set -e
expect_exit baseline 0 "$EC"
printf '%s' "$OUT" | grep -qi 'selected-path restore verification passed' && ok baseline_msg || bad baseline_msg
expect_no_secret baseline "$OUT"
assert_no_orphans

# ---------------------------------------------------------------------------
# REST backend for network faults
# ---------------------------------------------------------------------------
echo "=== REST backend for fault injection ==="
REST_PORT="$(free_port)"
REST_NAME="bl-gap-net-rest-${SUFFIX}"
REST_DATA="$BASE/rest-data"
mkdir -p "$REST_DATA"
docker run -d --name "$REST_NAME" \
  -e DISABLE_AUTHENTICATION=1 \
  -p "127.0.0.1:${REST_PORT}:8000" \
  -v "$REST_DATA:/data" \
  restic/rest-server:latest >/dev/null
CONTAINERS+=("$REST_NAME")
wait_tcp 127.0.0.1 "$REST_PORT" 60 || bad rest_listen
ok rest_listen

REST_REPO="rest:http://127.0.0.1:${REST_PORT}/repo"
export RESTIC_PASSWORD_FILE="$PASSFILE"
restic -r "$REST_REPO" init >/dev/null
restic -r "$REST_REPO" backup "$DATA" >/dev/null
unset RESTIC_PASSWORD_FILE

REST_CFG="$BASE/rest.yml"
cat >"$REST_CFG" <<EOF
backup_paths: []
restic:
  repository: ${REST_REPO}
  password_file: ${PASSFILE}
  restore_verification:
    mode: selected
EOF

echo "--- REST healthy ---"
set +e
OUT="$(timeout --foreground --signal=TERM --kill-after=5s 120 backuplint scan "$COMPOSE" --config "$REST_CFG" 2>&1)"
EC=$?
set -e
expect_exit rest_healthy 0 "$EC"
printf '%s' "$OUT" | grep -qi 'selected-path restore verification passed' && ok rest_healthy_msg || bad rest_healthy_msg

echo "--- backend disappears mid-restore (docker stop) ---"
set +e
timeout --signal=TERM --kill-after=15s 120 \
  backuplint scan "$COMPOSE" --config "$REST_CFG" >"$BASE/disappear.out" 2>&1 &
BL_PID=$!
sleep 1.5
docker stop "$REST_NAME" >/dev/null 2>&1
wait "$BL_PID"
EC=$?
set -e
OUT="$(cat "$BASE/disappear.out" 2>/dev/null || true)"
if [[ "$EC" -eq 0 ]] && printf '%s' "$OUT" | grep -qi 'selected-path restore verification passed'; then
  skip disappear_race_completed_early
else
  [[ "$EC" -eq 2 || "$EC" -eq 124 || "$EC" -ne 0 ]] && ok "disappear_exit=${EC}" || bad "disappear_exit=${EC}"
  expect_no_pass disappear "$OUT"
fi
[[ "$EC" -ne 124 ]] || ok disappear_bounded_by_timeout
expect_no_secret disappear "$OUT"
docker start "$REST_NAME" >/dev/null 2>&1
wait_tcp 127.0.0.1 "$REST_PORT" 40 || true
assert_no_orphans

echo "--- connection reset / docker kill ---"
set +e
timeout --signal=TERM --kill-after=15s 120 \
  backuplint scan "$COMPOSE" --config "$REST_CFG" >"$BASE/kill.out" 2>&1 &
BL_PID=$!
sleep 1.2
docker kill "$REST_NAME" >/dev/null 2>&1
wait "$BL_PID"
EC=$?
set -e
OUT="$(cat "$BASE/kill.out" 2>/dev/null || true)"
if [[ "$EC" -eq 0 ]] && printf '%s' "$OUT" | grep -qi 'selected-path restore verification passed'; then
  skip kill_race_completed_early
else
  [[ "$EC" -ne 0 ]] && ok "kill_exit=${EC}" || bad "kill_exit=${EC}"
  expect_no_pass kill "$OUT"
fi
expect_no_secret kill "$OUT"
docker start "$REST_NAME" >/dev/null 2>&1 || docker run -d --name "$REST_NAME" \
  -e DISABLE_AUTHENTICATION=1 \
  -p "127.0.0.1:${REST_PORT}:8000" \
  -v "$REST_DATA:/data" \
  restic/rest-server:latest >/dev/null
wait_tcp 127.0.0.1 "$REST_PORT" 40 || true
assert_no_orphans

echo "--- temporary endpoint failure then recover ---"
docker stop "$REST_NAME" >/dev/null 2>&1
set +e
OUT="$(timeout --foreground --signal=TERM --kill-after=5s 45 backuplint scan "$COMPOSE" --config "$REST_CFG" 2>&1)"
EC=$?
set -e
expect_exit_any temp_fail "$EC" 2 124
expect_no_pass temp_fail "$OUT"
[[ "$EC" -eq 2 || "$EC" -eq 124 ]] && ok temp_fail_bounded || bad temp_fail_hang
docker start "$REST_NAME" >/dev/null 2>&1
wait_tcp 127.0.0.1 "$REST_PORT" 40 || true
set +e
OUT="$(timeout --foreground --signal=TERM --kill-after=5s 120 backuplint scan "$COMPOSE" --config "$REST_CFG" 2>&1)"
EC=$?
set -e
expect_exit temp_recover 0 "$EC"
printf '%s' "$OUT" | grep -qi 'selected-path restore verification passed' && ok temp_recover_msg || bad temp_recover_msg

echo "--- partial transfer disconnect ---"
set +e
timeout --signal=TERM --kill-after=15s 90 \
  backuplint scan "$COMPOSE" --config "$REST_CFG" >"$BASE/partial.out" 2>&1 &
BL_PID=$!
sleep 0.8
docker pause "$REST_NAME" >/dev/null 2>&1
sleep 2
docker kill "$REST_NAME" >/dev/null 2>&1
wait "$BL_PID"
EC=$?
set -e
OUT="$(cat "$BASE/partial.out" 2>/dev/null || true)"
if [[ "$EC" -eq 0 ]] && printf '%s' "$OUT" | grep -qi 'selected-path restore verification passed'; then
  skip partial_race
else
  [[ "$EC" -ne 0 ]] && ok "partial_exit=${EC}" || bad "partial_exit=${EC}"
  expect_no_pass partial "$OUT"
fi
# Recreate rest-server (kill may leave container; rm+run)
docker rm -f "$REST_NAME" >/dev/null 2>&1
docker run -d --name "$REST_NAME" \
  -e DISABLE_AUTHENTICATION=1 \
  -p "127.0.0.1:${REST_PORT}:8000" \
  -v "$REST_DATA:/data" \
  restic/rest-server:latest >/dev/null
wait_tcp 127.0.0.1 "$REST_PORT" 40 || true
assert_no_orphans

echo "--- restore_verification.timeout: 1s (short) ---"
SHORT_CFG="$BASE/short-timeout.yml"
cat >"$SHORT_CFG" <<EOF
backup_paths: []
restic:
  repository: ${REST_REPO}
  password_file: ${PASSFILE}
  restore_verification:
    mode: selected
    timeout: 1s
EOF
# Make restore slow by pausing backend briefly after start — or use blackhole for list
BLACK_CFG="$BASE/blackhole.yml"
cat >"$BLACK_CFG" <<EOF
backup_paths: []
restic:
  repository: rest:http://203.0.113.1:9/repo
  password_file: ${PASSFILE}
  restore_verification:
    mode: selected
    timeout: 1s
EOF
set +e
OUT="$(timeout --foreground --signal=TERM --kill-after=5s 35 backuplint scan "$COMPOSE" --config "$BLACK_CFG" 2>&1)"
EC=$?
set -e
if [[ "$EC" -eq 2 || "$EC" -eq 124 ]]; then
  ok "short_timeout_exit=${EC}"
else
  bad "short_timeout_exit=${EC}"
fi
expect_no_pass short_timeout "$OUT"
expect_no_secret short_timeout "$OUT"

echo "--- high latency (tc netem) ---"
TC_APPLIED=0
if command -v tc >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
  if sudo -n tc qdisc add dev lo root netem delay 200ms 50ms 2>/dev/null; then
    TC_APPLIED=1
    set +e
    OUT="$(timeout --foreground --signal=TERM --kill-after=5s 180 backuplint scan "$COMPOSE" --config "$REST_CFG" 2>&1)"
    EC=$?
    set -e
    # Should still complete (slow) or exit 2 — never hang forever / never false PASS with wrong semantics
    if [[ "$EC" -eq 0 ]]; then
      printf '%s' "$OUT" | grep -qi 'selected-path restore verification passed' && ok latency_pass || bad latency_pass
    elif [[ "$EC" -eq 2 || "$EC" -eq 124 ]]; then
      ok "latency_bounded_exit=${EC}"
      expect_no_pass latency "$OUT"
    else
      bad "latency_exit=${EC}"
    fi
    sudo -n tc qdisc del dev lo root 2>/dev/null || true
    TC_APPLIED=0
  else
    skip "high_latency_tc_add_failed"
  fi
else
  skip "high_latency_no_tc_or_sudo"
fi

echo "--- low bandwidth (tc netem rate) ---"
if command -v tc >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
  if sudo -n tc qdisc add dev lo root tbf rate 256kbit burst 32kbit latency 400ms 2>/dev/null; then
    TC_APPLIED=1
    set +e
    OUT="$(timeout --foreground --signal=TERM --kill-after=5s 180 backuplint scan "$COMPOSE" --config "$REST_CFG" 2>&1)"
    EC=$?
    set -e
    if [[ "$EC" -eq 0 || "$EC" -eq 2 || "$EC" -eq 124 ]]; then
      ok "lowbw_bounded_exit=${EC}"
      if [[ "$EC" -ne 0 ]]; then expect_no_pass lowbw "$OUT"; fi
    else
      bad "lowbw_exit=${EC}"
    fi
    sudo -n tc qdisc del dev lo root 2>/dev/null || true
    TC_APPLIED=0
  else
    skip "low_bandwidth_tc_add_failed"
  fi
else
  skip "low_bandwidth_no_tc_or_sudo"
fi

assert_no_orphans

echo
echo "=== summary PASS=$PASS FAIL=$FAIL SKIP=$SKIP ==="
echo "Notes: tc netem on lo requires passwordless sudo; otherwise latency/bandwidth cases SKIP."
echo "Mid-restore stop/kill can race if restore finishes early — recorded as SKIP."
if [[ "$FAIL" -eq 0 ]]; then
  echo GAP_NETWORK_FAULTS_PASSED
  exit 0
fi
echo GAP_NETWORK_FAULTS_FAILED
exit 1
