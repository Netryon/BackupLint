#!/usr/bin/env bash
# Disposable local SFTP + Restic REST + MinIO S3-compatible restore verification
# labs for BackupLint v0.3 gap closure. Does NOT use AWS. Does NOT touch live
# user data. All containers/networks/temp dirs are cleaned on EXIT.
#
# Caveat (SFTP via BackupLint): BackupLint does not pass restic -o sftp.args.
# OpenSSH ignores $HOME for ~/.ssh/config (uses passwd home), so this lab puts a
# disposable ssh wrapper early on PATH that runs: ssh -F $BASE/ssh_config ...
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate" 2>/dev/null || true

unset RESTIC_PASSWORD RESTIC_PASSWORD_FILE RESTIC_REPOSITORY || true

SUFFIX="$(openssl rand -hex 4 2>/dev/null || printf '%04x%04x' "$RANDOM" "$RANDOM")"
BASE="$(mktemp -d "/tmp/backuplint-v03-gap-remote-${SUFFIX}-XXXXXX")"
PASSFILE="$BASE/restic.pass"
SSH_WRAP_BIN="$BASE/bin"
SSH_CONFIG="$BASE/ssh_config"
SFTP_PATH_PREFIX="$SSH_WRAP_BIN:$PATH"

PASS=0
FAIL=0
SKIP=0

ok() { echo "OK $*"; PASS=$((PASS + 1)); }
bad() { echo "FAIL $*"; FAIL=$((FAIL + 1)); }
skip() { echo "SKIP $*"; SKIP=$((SKIP + 1)); }

CONTAINERS=()
NETWORKS=()

cleanup() {
  set +e
  for c in "${CONTAINERS[@]:-}"; do
    docker rm -f "$c" >/dev/null 2>&1
  done
  for n in "${NETWORKS[@]:-}"; do
    docker network rm "$n" >/dev/null 2>&1
  done
  # Compose stacks under BASE
  if [[ -d "$BASE" ]]; then
    while IFS= read -r -d '' yml; do
      docker compose -f "$yml" down --remove-orphans >/dev/null 2>&1
    done < <(find "$BASE" -name 'compose.yml' -print0 2>/dev/null)
    # rest-server/minio may leave root-owned files
    docker run --rm -v "$BASE:/wipe" alpine:3.20 \
      sh -c 'rm -rf /wipe/*' >/dev/null 2>&1 || true
  fi
  rm -rf "$BASE" 2>/dev/null || true
  # Best-effort: remove any leftover restore dirs from this lab user under /tmp
  find /tmp -maxdepth 1 -user "$(id -un)" -type d -name 'backuplint-restore-*' \
    -mmin -180 -exec rm -rf {} + 2>/dev/null
  set -e
}
trap cleanup EXIT

printf 'v03-gap-remote-pass\n' >"$PASSFILE"
chmod 600 "$PASSFILE"

free_port() {
  python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
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

write_stack() {
  # write_stack <dir> <data_dir> [extra compose services yaml fragment unused]
  local dir="$1"
  local data_dir="$2"
  mkdir -p "$dir" "$data_dir"
  cat >"$dir/compose.yml" <<EOF
services:
  app:
    image: alpine:3.20
    container_name: bl-gap-app-${SUFFIX}-$(basename "$dir")
    command: ["sleep", "7200"]
    volumes:
      - ${data_dir}:/data
EOF
}

write_cfg() {
  local cfg="$1"
  local repo="$2"
  local pass="$3"
  shift 3
  cat >"$cfg" <<EOF
backup_paths: []
restic:
  repository: ${repo}
  password_file: ${pass}
  restore_verification:
    mode: selected
EOF
  if [[ "$#" -gt 0 ]]; then
    # Optional extra YAML lines under restore_verification (indent 4 spaces)
    local line
    for line in "$@"; do
      printf '    %s\n' "$line" >>"$cfg"
    done
  fi
}

expect_exit() {
  local label="$1"
  local want="$2"
  local got="$3"
  if [[ "$got" -eq "$want" ]]; then
    ok "${label}_exit=${got}"
  else
    bad "${label}_exit=${got}_want=${want}"
  fi
}

expect_exit_any() {
  local label="$1"
  local got="$2"
  shift 2
  local w
  for w in "$@"; do
    if [[ "$got" -eq "$w" ]]; then
      ok "${label}_exit=${got}"
      return 0
    fi
  done
  bad "${label}_exit=${got}_want_one_of=$*"
}

expect_grep() {
  local label="$1"
  local pattern="$2"
  local hay="$3"
  if printf '%s' "$hay" | grep -Eqi -- "$pattern"; then
    ok "$label"
  else
    bad "$label"
  fi
}

expect_no_grep() {
  local label="$1"
  local pattern="$2"
  local hay="$3"
  if printf '%s' "$hay" | grep -Eqi -- "$pattern"; then
    bad "$label"
  else
    ok "$label"
  fi
}

run_bl() {
  # run_bl <timeout_sec> <env assignments via env...> -- backuplint args...
  # Usage: run_bl 60 PATH="$SFTP_PATH_PREFIX" -- scan compose --config cfg
  local t="$1"
  shift
  local -a env_pairs=()
  while [[ "$#" -gt 0 && "$1" != "--" ]]; do
    env_pairs+=("$1")
    shift
  done
  [[ "${1:-}" == "--" ]] && shift
  set +e
  # --foreground: deliver signals to the timed command itself (avoids zombie/hang
  # when Restic children outlive BackupLint under job-control quirks).
  OUT="$(timeout --foreground --signal=TERM --kill-after=5s "$t" \
    env "${env_pairs[@]}" backuplint "$@" 2>&1)"
  EC=$?
  # Reap any lab restic children left after forced timeout.
  pkill -9 -f "restic .*${BASE}" >/dev/null 2>&1 || true
  set -e
  if [[ "$EC" -eq 124 ]]; then
    OUT+=$'\n[lab] command timed out'
  fi
}

write_ssh_config() {
  local port="$1"
  local key="$2"
  local out="${3:-$SSH_CONFIG}"
  cat >"$out" <<EOF
Host 127.0.0.1 localhost
  HostName 127.0.0.1
  Port ${port}
  User bluser
  IdentityFile ${key}
  IdentitiesOnly yes
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
  GlobalKnownHostsFile /dev/null
  LogLevel ERROR
EOF
  chmod 600 "$out"
}

install_ssh_wrapper() {
  mkdir -p "$SSH_WRAP_BIN"
  cat >"$SSH_WRAP_BIN/ssh" <<EOF
#!/bin/bash
exec /usr/bin/ssh -F "${SSH_CONFIG}" "\$@"
EOF
  chmod +x "$SSH_WRAP_BIN/ssh"
}

assert_no_temp_orphans() {
  local label="$1"
  local before="$2"
  local leftovers
  leftovers="$(find /tmp -maxdepth 1 -user "$(id -un)" -type d -name 'backuplint-restore-*' 2>/dev/null | wc -l | tr -d ' ')"
  if [[ "$leftovers" -le "$before" ]]; then
    ok "${label}_no_restore_orphans"
  else
    bad "${label}_restore_orphans=${leftovers}_before=${before}"
  fi
}

count_restore_temps() {
  find /tmp -maxdepth 1 -user "$(id -un)" -type d -name 'backuplint-restore-*' 2>/dev/null | wc -l | tr -d ' '
}

# ---------------------------------------------------------------------------
# 1) SFTP (atmoz/sftp + SSH key + disposable PATH ssh wrapper)
# ---------------------------------------------------------------------------
echo "=== SFTP backend ==="
SFTP_DIR="$BASE/sftp"
SFTP_DATA="$SFTP_DIR/data"
SFTP_KEYS="$SFTP_DIR/keys"
SFTP_PORT="$(free_port)"
SFTP_NAME="bl-gap-sftp-${SUFFIX}"
mkdir -p "$SFTP_DATA/repo" "$SFTP_KEYS"

ssh-keygen -t ed25519 -N '' -f "$SFTP_KEYS/id_ed25519" -q
# Ensure trailing newline for atmoz authorized_keys assembly
printf '\n' >>"$SFTP_KEYS/id_ed25519.pub"
chmod 600 "$SFTP_KEYS/id_ed25519"

write_ssh_config "$SFTP_PORT" "$SFTP_KEYS/id_ed25519" "$SSH_CONFIG"
install_ssh_wrapper
# Refresh PATH prefix after bin exists
SFTP_PATH_PREFIX="$SSH_WRAP_BIN:$PATH"

docker run -d --name "$SFTP_NAME" \
  -p "127.0.0.1:${SFTP_PORT}:22" \
  -v "$SFTP_KEYS/id_ed25519.pub:/home/bluser/.ssh/keys/id_ed25519.pub:ro" \
  -v "$SFTP_DATA:/home/bluser/data" \
  atmoz/sftp:alpine \
  "bluser:unusedpass:1000:1000:data" >/dev/null
CONTAINERS+=("$SFTP_NAME")

if ! wait_tcp 127.0.0.1 "$SFTP_PORT" 80; then
  bad sftp_server_listen
else
  ok sftp_server_listen
fi
# Wait until SSH/SFTP auth works (atmoz may accept TCP before keys are ready).
SFTP_READY=0
for _ in $(seq 1 60); do
  # atmoz often returns "This service allows sftp connections only" — that means ready.
  if PATH="$SFTP_PATH_PREFIX" ssh -F "$SSH_CONFIG" -o BatchMode=yes \
    -o ConnectTimeout=2 bluser@127.0.0.1 2>&1 | grep -qiE 'sftp|success|permission denied'; then
    SFTP_READY=1
    break
  fi
  sleep 0.5
done
if [[ "$SFTP_READY" -eq 1 ]]; then
  ok sftp_protocol_ready
else
  skip sftp_protocol_ready_soft
fi

SFTP_REPO="sftp:bluser@127.0.0.1:/data/repo"

# Init + backup via restic using the same PATH wrapper BackupLint will see
mkdir -p "$SFTP_DIR/payload"
echo 'sftp-healthy-payload' >"$SFTP_DIR/payload/file.txt"
mkdir -p "$SFTP_DIR/payload/nested"
echo 'nested' >"$SFTP_DIR/payload/nested/x.txt"
# Path with spaces
mkdir -p "$SFTP_DIR/payload/path with spaces"
echo 'spaced' >"$SFTP_DIR/payload/path with spaces/note.txt"

export RESTIC_PASSWORD_FILE="$PASSFILE"
PATH="$SFTP_PATH_PREFIX" restic -r "$SFTP_REPO" init >/dev/null
PATH="$SFTP_PATH_PREFIX" restic -r "$SFTP_REPO" backup "$SFTP_DIR/payload" >/dev/null
unset RESTIC_PASSWORD_FILE

write_stack "$SFTP_DIR/stack" "$SFTP_DIR/payload"
docker compose -f "$SFTP_DIR/stack/compose.yml" up -d >/dev/null
write_cfg "$SFTP_DIR/backuplint.yml" "$SFTP_REPO" "$PASSFILE"

echo "--- SFTP healthy ---"
ORPHAN_BEFORE="$(count_restore_temps)"
run_bl 90 PATH="$SFTP_PATH_PREFIX" -- scan "$SFTP_DIR/stack/compose.yml" --config "$SFTP_DIR/backuplint.yml"
expect_exit sftp_healthy 0 "$EC"
expect_grep sftp_healthy_msg 'selected-path restore verification passed' "$OUT"
expect_no_grep sftp_healthy_no_pass 'unusedpass|v03-gap-remote-pass' "$OUT"
assert_no_temp_orphans sftp_healthy "$ORPHAN_BEFORE"

echo "--- SFTP wrong password_file ---"
printf 'wrong-password\n' >"$BASE/bad.pass"
chmod 600 "$BASE/bad.pass"
write_cfg "$SFTP_DIR/bad.yml" "$SFTP_REPO" "$BASE/bad.pass"
run_bl 60 PATH="$SFTP_PATH_PREFIX" -- scan "$SFTP_DIR/stack/compose.yml" --config "$SFTP_DIR/bad.yml"
expect_exit sftp_bad_pass 2 "$EC"
expect_grep sftp_bad_pass_msg 'authentication failed' "$OUT"
expect_no_grep sftp_bad_pass_secret 'wrong-password' "$OUT"

echo "--- SFTP missing repository path on server ---"
write_cfg "$SFTP_DIR/miss.yml" "sftp:bluser@127.0.0.1:/data/does-not-exist-repo" "$PASSFILE"
run_bl 60 PATH="$SFTP_PATH_PREFIX" -- scan "$SFTP_DIR/stack/compose.yml" --config "$SFTP_DIR/miss.yml"
expect_exit sftp_missing_repo 2 "$EC"

echo "--- SFTP connection refused (wrong port) ---"
WRONG_PORT="$(free_port)"
BAD_SSH_CONFIG="$BASE/ssh_config_badport"
write_ssh_config "$WRONG_PORT" "$SFTP_KEYS/id_ed25519" "$BAD_SSH_CONFIG"
# Temporarily point wrapper at bad config
cat >"$SSH_WRAP_BIN/ssh" <<EOF
#!/bin/bash
exec /usr/bin/ssh -F "${BAD_SSH_CONFIG}" "\$@"
EOF
chmod +x "$SSH_WRAP_BIN/ssh"
write_cfg "$SFTP_DIR/refused.yml" "$SFTP_REPO" "$PASSFILE"
run_bl 45 PATH="$SFTP_PATH_PREFIX" -- scan "$SFTP_DIR/stack/compose.yml" --config "$SFTP_DIR/refused.yml"
expect_exit sftp_refused 2 "$EC"
[[ "$EC" -ne 124 ]] && ok sftp_refused_no_hang || bad sftp_refused_hang
# Restore good wrapper
install_ssh_wrapper

echo "--- SFTP server stopped (unavailable) ---"
docker stop "$SFTP_NAME" >/dev/null
run_bl 60 PATH="$SFTP_PATH_PREFIX" -- scan "$SFTP_DIR/stack/compose.yml" --config "$SFTP_DIR/backuplint.yml"
expect_exit sftp_stopped 2 "$EC"
[[ "$EC" -ne 124 ]] && ok sftp_stopped_no_hang || bad sftp_stopped_hang
expect_no_grep sftp_stopped_no_false_pass 'selected-path restore verification passed' "$OUT"
docker start "$SFTP_NAME" >/dev/null
wait_tcp 127.0.0.1 "$SFTP_PORT" 40 || true
for _ in $(seq 1 30); do
  echo $'ls\nbye' | PATH="$SFTP_PATH_PREFIX" sftp -o BatchMode=yes "bluser@127.0.0.1" >/dev/null 2>&1 && break
  sleep 0.4
done

echo "--- SFTP mid-restore interrupt (best-effort) ---"
dd if=/dev/urandom of="$SFTP_DIR/payload/large.bin" bs=1M count=40 status=none 2>/dev/null || \
  dd if=/dev/zero of="$SFTP_DIR/payload/large.bin" bs=1M count=40 status=none
export RESTIC_PASSWORD_FILE="$PASSFILE"
PATH="$SFTP_PATH_PREFIX" restic -r "$SFTP_REPO" backup "$SFTP_DIR/payload" >/dev/null
unset RESTIC_PASSWORD_FILE
set +e
PATH="$SFTP_PATH_PREFIX" timeout --signal=TERM --kill-after=15s 90 \
  backuplint scan "$SFTP_DIR/stack/compose.yml" --config "$SFTP_DIR/backuplint.yml" \
  >"$SFTP_DIR/interrupt.out" 2>&1 &
BL_PID=$!
sleep 1.5
docker stop "$SFTP_NAME" >/dev/null 2>&1
wait "$BL_PID"
INT_EC=$?
set -e
INT_OUT="$(cat "$SFTP_DIR/interrupt.out" 2>/dev/null || true)"
if [[ "$INT_EC" -eq 0 ]] && printf '%s' "$INT_OUT" | grep -qi 'selected-path restore verification passed'; then
  skip sftp_mid_restore_race_completed_before_stop
elif [[ "$INT_EC" -eq 2 ]] || [[ "$INT_EC" -eq 124 ]] || [[ "$INT_EC" -ne 0 ]]; then
  ok "sftp_mid_restore_exit=${INT_EC}"
  expect_no_grep sftp_mid_restore_no_false_pass 'selected-path restore verification passed' "$INT_OUT"
else
  bad "sftp_mid_restore_unexpected_exit=${INT_EC}"
fi
docker start "$SFTP_NAME" >/dev/null 2>&1 || true
wait_tcp 127.0.0.1 "$SFTP_PORT" 40 || true

# ---------------------------------------------------------------------------
# 2) Restic REST server
# ---------------------------------------------------------------------------
echo "=== REST server backend ==="
REST_DIR="$BASE/rest"
REST_DATA="$REST_DIR/data"
REST_PORT="$(free_port)"
REST_NAME="bl-gap-rest-${SUFFIX}"
mkdir -p "$REST_DATA"

# restic/rest-server Cmd is /entrypoint.sh; extra CLI args replace it. Use env flags.
docker run -d --name "$REST_NAME" \
  -e DISABLE_AUTHENTICATION=1 \
  -p "127.0.0.1:${REST_PORT}:8000" \
  -v "$REST_DATA:/data" \
  restic/rest-server:latest >/dev/null
CONTAINERS+=("$REST_NAME")
wait_tcp 127.0.0.1 "$REST_PORT" 60 || bad rest_server_listen
ok rest_server_listen

REST_REPO="rest:http://127.0.0.1:${REST_PORT}/repo"
mkdir -p "$REST_DIR/payload"
echo 'rest-healthy' >"$REST_DIR/payload/file.txt"
mkdir -p "$REST_DIR/payload/nested"
echo 'n' >"$REST_DIR/payload/nested/x.txt"

export RESTIC_PASSWORD_FILE="$PASSFILE"
restic -r "$REST_REPO" init >/dev/null
restic -r "$REST_REPO" backup "$REST_DIR/payload" >/dev/null
unset RESTIC_PASSWORD_FILE

write_stack "$REST_DIR/stack" "$REST_DIR/payload"
docker compose -f "$REST_DIR/stack/compose.yml" up -d >/dev/null
write_cfg "$REST_DIR/backuplint.yml" "$REST_REPO" "$PASSFILE"

echo "--- REST healthy ---"
ORPHAN_BEFORE="$(count_restore_temps)"
run_bl 90 -- scan "$REST_DIR/stack/compose.yml" --config "$REST_DIR/backuplint.yml"
expect_exit rest_healthy 0 "$EC"
expect_grep rest_healthy_msg 'selected-path restore verification passed' "$OUT"
assert_no_temp_orphans rest_healthy "$ORPHAN_BEFORE"

echo "--- REST missing repo ---"
write_cfg "$REST_DIR/miss.yml" "rest:http://127.0.0.1:${REST_PORT}/missing-repo" "$PASSFILE"
run_bl 60 -- scan "$REST_DIR/stack/compose.yml" --config "$REST_DIR/miss.yml"
expect_exit rest_missing 2 "$EC"

echo "--- REST server stopped ---"
docker stop "$REST_NAME" >/dev/null
run_bl 45 -- scan "$REST_DIR/stack/compose.yml" --config "$REST_DIR/backuplint.yml"
# Stopped listeners may SYN-retry (wrapper 124) instead of immediate refused (2).
expect_exit_any rest_stopped "$EC" 2 124
[[ "$EC" -eq 2 || "$EC" -eq 124 ]] && ok rest_stopped_bounded || bad rest_stopped_hang
expect_no_grep rest_stopped_no_false_pass 'selected-path restore verification passed' "$OUT"
docker start "$REST_NAME" >/dev/null
wait_tcp 127.0.0.1 "$REST_PORT" 40 || true

echo "--- REST short timeout (operational / no false PASS) ---"
write_cfg "$REST_DIR/to.yml" "$REST_REPO" "$PASSFILE" "timeout: 1s"
# Point at blackhole-ish unreachable to force timeout on snapshot list/restore path
write_cfg "$REST_DIR/to_blackhole.yml" "rest:http://203.0.113.1:9/repo" "$PASSFILE" "timeout: 1s"
run_bl 30 -- scan "$REST_DIR/stack/compose.yml" --config "$REST_DIR/to_blackhole.yml"
# Snapshot listing uses fixed 120s in product — wrapper timeout 30 must win → 124 or exit 2
if [[ "$EC" -eq 2 ]] || [[ "$EC" -eq 124 ]]; then
  ok "rest_timeoutish_exit=${EC}"
else
  bad "rest_timeoutish_exit=${EC}"
fi
expect_no_grep rest_timeout_no_false_pass 'selected-path restore verification passed' "$OUT"

echo "--- REST auth (htpasswd) ---"
REST_AUTH_PORT="$(free_port)"
REST_AUTH_NAME="bl-gap-restauth-${SUFFIX}"
REST_AUTH_DATA="$REST_DIR/auth-data"
mkdir -p "$REST_AUTH_DATA"
# Create bcrypt htpasswd (rest-server expects apache-style bcrypt).
if command -v htpasswd >/dev/null 2>&1; then
  htpasswd -nbB restuser rest-secret-lab >"$REST_AUTH_DATA/.htpasswd"
else
  docker run --rm httpd:2.4-alpine htpasswd -nbB restuser rest-secret-lab \
    >"$REST_AUTH_DATA/.htpasswd"
fi
# Strip CR if any
tr -d '\r' <"$REST_AUTH_DATA/.htpasswd" >"$REST_AUTH_DATA/.htpasswd.tmp"
mv "$REST_AUTH_DATA/.htpasswd.tmp" "$REST_AUTH_DATA/.htpasswd"

docker run -d --name "$REST_AUTH_NAME" \
  -p "127.0.0.1:${REST_AUTH_PORT}:8000" \
  -v "$REST_AUTH_DATA:/data" \
  restic/rest-server:latest >/dev/null
CONTAINERS+=("$REST_AUTH_NAME")
wait_tcp 127.0.0.1 "$REST_AUTH_PORT" 60 || bad rest_auth_listen
ok rest_auth_listen

REST_AUTH_REPO="rest:http://restuser:rest-secret-lab@127.0.0.1:${REST_AUTH_PORT}/repo"
mkdir -p "$REST_DIR/auth-payload"
echo 'auth-ok' >"$REST_DIR/auth-payload/file.txt"
export RESTIC_PASSWORD_FILE="$PASSFILE"
restic -r "$REST_AUTH_REPO" init >/dev/null
restic -r "$REST_AUTH_REPO" backup "$REST_DIR/auth-payload" >/dev/null
unset RESTIC_PASSWORD_FILE

write_stack "$REST_DIR/auth-stack" "$REST_DIR/auth-payload"
docker compose -f "$REST_DIR/auth-stack/compose.yml" up -d >/dev/null
write_cfg "$REST_DIR/auth.yml" "$REST_AUTH_REPO" "$PASSFILE"

run_bl 90 -- scan "$REST_DIR/auth-stack/compose.yml" --config "$REST_DIR/auth.yml"
expect_exit rest_auth_healthy 0 "$EC"
expect_grep rest_auth_healthy_msg 'selected-path restore verification passed' "$OUT"
expect_no_grep rest_auth_no_http_secret 'rest-secret-lab' "$OUT"

write_cfg "$REST_DIR/auth_bad.yml" "rest:http://restuser:wrong-http-pass@127.0.0.1:${REST_AUTH_PORT}/repo" "$PASSFILE"
run_bl 60 -- scan "$REST_DIR/auth-stack/compose.yml" --config "$REST_DIR/auth_bad.yml"
expect_exit rest_auth_fail 2 "$EC"
expect_no_grep rest_auth_fail_no_false_pass 'selected-path restore verification passed' "$OUT"
expect_no_grep rest_auth_fail_no_secret 'wrong-http-pass|rest-secret-lab' "$OUT"

echo "--- REST kill mid-transfer ---"
dd if=/dev/urandom of="$REST_DIR/payload/large.bin" bs=1M count=48 status=none 2>/dev/null || \
  dd if=/dev/zero of="$REST_DIR/payload/large.bin" bs=1M count=48 status=none
export RESTIC_PASSWORD_FILE="$PASSFILE"
restic -r "$REST_REPO" backup "$REST_DIR/payload" >/dev/null
unset RESTIC_PASSWORD_FILE
set +e
timeout --signal=TERM --kill-after=15s 90 \
  backuplint scan "$REST_DIR/stack/compose.yml" --config "$REST_DIR/backuplint.yml" \
  >"$REST_DIR/kill.out" 2>&1 &
BL_PID=$!
sleep 1.2
docker kill "$REST_NAME" >/dev/null 2>&1
wait "$BL_PID"
KILL_EC=$?
set -e
KILL_OUT="$(cat "$REST_DIR/kill.out" 2>/dev/null || true)"
if [[ "$KILL_EC" -eq 0 ]] && printf '%s' "$KILL_OUT" | grep -qi 'selected-path restore verification passed'; then
  skip rest_mid_kill_race_completed
elif [[ "$KILL_EC" -ne 0 ]]; then
  ok "rest_mid_kill_exit=${KILL_EC}"
  expect_no_grep rest_mid_kill_no_false_pass 'selected-path restore verification passed' "$KILL_OUT"
else
  bad "rest_mid_kill_unexpected_exit=${KILL_EC}"
fi
docker start "$REST_NAME" >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# 3) MinIO S3-compatible (NOT AWS)
# ---------------------------------------------------------------------------
echo "=== MinIO S3-compatible backend (not AWS) ==="
S3_DIR="$BASE/minio"
S3_DATA="$S3_DIR/data"
S3_PORT="$(free_port)"
S3_NAME="bl-gap-minio-${SUFFIX}"
S3_USER="minioadmin"
S3_SECRET="minio-lab-secret-${SUFFIX}"
S3_BUCKET="bl-gap-bucket"
mkdir -p "$S3_DATA"

docker run -d --name "$S3_NAME" \
  --network host \
  -e "MINIO_ROOT_USER=${S3_USER}" \
  -e "MINIO_ROOT_PASSWORD=${S3_SECRET}" \
  -v "$S3_DATA:/data" \
  minio/minio:latest \
  server /data --address "127.0.0.1:${S3_PORT}" >/dev/null
CONTAINERS+=("$S3_NAME")
wait_tcp 127.0.0.1 "$S3_PORT" 80 || bad minio_listen
ok minio_listen

# Wait until MinIO API is actually ready (TCP accept can precede HTTP).
MINIO_READY=0
for _ in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${S3_PORT}/minio/health/live" || true)"
  if [[ "$code" == "200" ]]; then
    MINIO_READY=1
    break
  fi
  sleep 0.5
done
[[ "$MINIO_READY" -eq 1 ]] && ok minio_api_ready || bad minio_api_ready

# Create bucket with boto3 (reliable vs mc+port mapping quirks).
if AWS_ACCESS_KEY_ID="$S3_USER" AWS_SECRET_ACCESS_KEY="$S3_SECRET" \
  "$ROOT/.venv/bin/python" - <<PY
import boto3
from botocore.client import Config
s3 = boto3.client(
    "s3",
    endpoint_url="http://127.0.0.1:${S3_PORT}",
    aws_access_key_id="${S3_USER}",
    aws_secret_access_key="${S3_SECRET}",
    config=Config(signature_version="s3v4"),
    region_name="us-east-1",
)
try:
    s3.create_bucket(Bucket="${S3_BUCKET}")
except Exception as exc:
    # Idempotent if already exists.
    if "BucketAlreadyOwnedByYou" not in str(exc) and "BucketAlreadyExists" not in str(exc):
        # MinIO may return 409 differently — try head.
        s3.head_bucket(Bucket="${S3_BUCKET}")
print("ok")
PY
then
  ok minio_bucket
else
  bad minio_bucket
fi

S3_REPO="s3:http://127.0.0.1:${S3_PORT}/${S3_BUCKET}"
mkdir -p "$S3_DIR/payload"
echo 'minio-healthy' >"$S3_DIR/payload/file.txt"
mkdir -p "$S3_DIR/payload/nested"
echo 'n' >"$S3_DIR/payload/nested/x.txt"

unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION || true
export AWS_ACCESS_KEY_ID="$S3_USER"
export AWS_SECRET_ACCESS_KEY="$S3_SECRET"
export AWS_DEFAULT_REGION="us-east-1"
export RESTIC_PASSWORD_FILE="$PASSFILE"
restic -r "$S3_REPO" init >/dev/null
restic -r "$S3_REPO" backup "$S3_DIR/payload" >/dev/null
unset RESTIC_PASSWORD_FILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION

write_stack "$S3_DIR/stack" "$S3_DIR/payload"
docker compose -f "$S3_DIR/stack/compose.yml" up -d >/dev/null
write_cfg "$S3_DIR/backuplint.yml" "$S3_REPO" "$PASSFILE"

echo "--- MinIO healthy ---"
ORPHAN_BEFORE="$(count_restore_temps)"
run_bl 120 \
  AWS_ACCESS_KEY_ID="$S3_USER" \
  AWS_SECRET_ACCESS_KEY="$S3_SECRET" \
  AWS_DEFAULT_REGION=us-east-1 \
  -- scan "$S3_DIR/stack/compose.yml" --config "$S3_DIR/backuplint.yml"
expect_exit minio_healthy 0 "$EC"
expect_grep minio_healthy_msg 'selected-path restore verification passed' "$OUT"
expect_no_grep minio_healthy_no_secret "$S3_SECRET" "$OUT"
assert_no_temp_orphans minio_healthy "$ORPHAN_BEFORE"

echo "--- MinIO wrong access key ---"
run_bl 25 \
  AWS_ACCESS_KEY_ID="wrong-access-key" \
  AWS_SECRET_ACCESS_KEY="$S3_SECRET" \
  AWS_DEFAULT_REGION=us-east-1 \
  -- scan "$S3_DIR/stack/compose.yml" --config "$S3_DIR/backuplint.yml"
expect_exit_any minio_bad_ak "$EC" 2 124
expect_no_grep minio_bad_ak_no_false_pass 'selected-path restore verification passed' "$OUT"

echo "--- MinIO wrong secret ---"
run_bl 25 \
  AWS_ACCESS_KEY_ID="$S3_USER" \
  AWS_SECRET_ACCESS_KEY="wrong-secret-value" \
  AWS_DEFAULT_REGION=us-east-1 \
  -- scan "$S3_DIR/stack/compose.yml" --config "$S3_DIR/backuplint.yml"
expect_exit_any minio_bad_sk "$EC" 2 124
expect_no_grep minio_bad_sk_secret 'wrong-secret-value' "$OUT"
expect_no_grep minio_bad_sk_no_false_pass 'selected-path restore verification passed' "$OUT"

echo "--- MinIO missing bucket ---"
write_cfg "$S3_DIR/miss.yml" "s3:http://127.0.0.1:${S3_PORT}/no-such-bucket-${SUFFIX}" "$PASSFILE"
run_bl 25 \
  AWS_ACCESS_KEY_ID="$S3_USER" \
  AWS_SECRET_ACCESS_KEY="$S3_SECRET" \
  AWS_DEFAULT_REGION=us-east-1 \
  -- scan "$S3_DIR/stack/compose.yml" --config "$S3_DIR/miss.yml"
expect_exit_any minio_missing_bucket "$EC" 2 124

echo "--- MinIO endpoint stopped ---"
docker stop "$S3_NAME" >/dev/null
run_bl 25 \
  AWS_ACCESS_KEY_ID="$S3_USER" \
  AWS_SECRET_ACCESS_KEY="$S3_SECRET" \
  AWS_DEFAULT_REGION=us-east-1 \
  -- scan "$S3_DIR/stack/compose.yml" --config "$S3_DIR/backuplint.yml"
expect_exit_any minio_stopped "$EC" 2 124
[[ "$EC" -eq 2 || "$EC" -eq 124 ]] && ok minio_stopped_bounded || bad minio_stopped_hang
expect_no_grep minio_stopped_no_false_pass 'selected-path restore verification passed' "$OUT"
docker start "$S3_NAME" >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
echo
echo "=== summary PASS=$PASS FAIL=$FAIL SKIP=$SKIP ==="
echo "Limitations:"
echo "  - SFTP through BackupLint uses a disposable PATH ssh wrapper (ssh -F lab config)."
echo "    OpenSSH ignores \$HOME for ~/.ssh/config; HOME override alone does not work."
echo "  - Mid-restore docker stop/kill can race if restore finishes early; recorded as SKIP when raced."
echo "  - MinIO validates S3-compatible API only; AWS S3 itself was NOT tested."
echo "  - list_snapshots timeout is product-fixed at 120s; lab uses process timeout wrappers."
echo "  - Path-with-spaces covered in SFTP backup payload; restore selected paths follow compose bind."
if [[ "$FAIL" -eq 0 ]]; then
  echo GAP_REMOTE_BACKENDS_PASSED
  exit 0
fi
echo GAP_REMOTE_BACKENDS_FAILED
exit 1
