#!/usr/bin/env bash
# Repeatable controller-container hardening harness.
# Usage (from repo root or any cwd):
#   ./deploy/controller/run-hardening-harness.sh
#
# Exit 0 on success. On failure, prints log directory path and leaves logs.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

IMAGE="${BACKUPLINT_HARNESS_IMAGE:-backuplint-controller:harness}"
SUFFIX="$(date +%s)-${RANDOM}"
VOLUME="bl-ctrl-harness-${SUFFIX}"
LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/bl-ctrl-harness.XXXXXX")"
KEEP_LOGS=0
CONTAINERS=()

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON=python3
fi

cleanup() {
  local c
  for c in "${CONTAINERS[@]:-}"; do
    docker rm -f "$c" >/dev/null 2>&1 || true
  done
  docker volume rm -f "$VOLUME" >/dev/null 2>&1 || true
  if [[ "$KEEP_LOGS" -eq 0 ]]; then
    rm -rf "$LOG_DIR"
  else
    echo "Preserved harness logs: $LOG_DIR" >&2
  fi
}
trap cleanup EXIT

fail() {
  KEEP_LOGS=1
  local c
  for c in "${CONTAINERS[@]:-}"; do
    docker logs "$c" >"$LOG_DIR/${c}.log" 2>&1 || true
  done
  echo "HARNESS FAIL: $* (logs: $LOG_DIR)" >&2
  exit 1
}

track() {
  CONTAINERS+=("$1")
}

command -v docker >/dev/null || fail "docker not available"
docker info >/dev/null 2>&1 || fail "docker daemon not available"

echo "==> Building $IMAGE"
docker build -f Dockerfile.controller -t "$IMAGE" . >"$LOG_DIR/build.log" 2>&1 \
  || fail "image build failed (see $LOG_DIR/build.log)"

docker volume create "$VOLUME" >/dev/null

run_hardened() {
  local cname=$1
  docker run -d --name "$cname" \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --tmpfs /tmp:size=32m,mode=1777 \
    -p 127.0.0.1::8443 \
    -e BACKUPLINT_CONTROLLER_HOSTNAME=localhost \
    -e HOME=/state \
    -e TMPDIR=/tmp \
    -v "${VOLUME}:/state" \
    "$IMAGE" >/dev/null
  track "$cname"
}

wait_healthy() {
  local cname=$1
  local i
  for i in $(seq 1 60); do
    if docker exec "$cname" python /app/docker/controller-healthcheck.py >/dev/null 2>&1; then
      return 0
    fi
    if [[ "$(docker inspect -f '{{.State.Running}}' "$cname" 2>/dev/null || true)" != "true" ]]; then
      return 1
    fi
    sleep 1
  done
  return 1
}

host_port() {
  docker inspect --format '{{(index (index .NetworkSettings.Ports "8443/tcp") 0).HostPort}}' "$1"
}

echo "==> Fresh container + fresh volume (hardened flags)"
NAME="bl-ctrl-harness-${SUFFIX}"
run_hardened "$NAME"
wait_healthy "$NAME" || fail "initial health failed"

echo "==> Non-root uid check"
UID_LINE="$(docker exec "$NAME" id -u)"
GID_LINE="$(docker exec "$NAME" id -g)"
[[ "$UID_LINE" == "10001" ]] || fail "expected uid 10001 got $UID_LINE"
[[ "$GID_LINE" == "10001" ]] || fail "expected gid 10001 got $GID_LINE"

echo "==> Version reporting"
docker exec "$NAME" backuplint --version >"$LOG_DIR/version.txt"
grep -q '.' "$LOG_DIR/version.txt" || fail "empty version"

echo "==> Enroll + revoke"
TOKEN="$(docker run --rm -v "${VOLUME}:/state" --entrypoint backuplint "$IMAGE" \
  controller enroll-token --data-dir /state --label harness)"
[[ -n "$TOKEN" ]] || fail "empty enroll token"

PORT="$(host_port "$NAME")"
docker cp "${NAME}:/state/ca/ca.crt" "$LOG_DIR/ca.crt"
AGENT_DIR="$LOG_DIR/agent"
mkdir -p "$AGENT_DIR"
PYTHONPATH=src "$PYTHON" -m backuplint agent enroll \
  --controller "https://127.0.0.1:${PORT}" \
  --token "$TOKEN" \
  --ca-cert "$LOG_DIR/ca.crt" \
  --identity-dir "$AGENT_DIR" >"$LOG_DIR/enroll.out" 2>"$LOG_DIR/enroll.err" \
  || fail "native enroll failed"
AGENT_ID="$(tr -d '[:space:]' <"$AGENT_DIR/agent_id")"
docker run --rm -v "${VOLUME}:/state" --entrypoint backuplint "$IMAGE" \
  controller revoke "$AGENT_ID" --data-dir /state >/dev/null

echo "==> SIGTERM stop + recreate"
docker stop -t 10 "$NAME" >/dev/null || fail "SIGTERM stop failed"
docker rm -f "$NAME" >/dev/null
CONTAINERS=("${CONTAINERS[@]/$NAME}")

NAME2="bl-ctrl-harness2-${SUFFIX}"
run_hardened "$NAME2"
wait_healthy "$NAME2" || fail "recreate health failed"

STATUS="$(docker run --rm -v "${VOLUME}:/state" --entrypoint backuplint "$IMAGE" \
  controller agents --data-dir /state)"
echo "$STATUS" | grep -q "$AGENT_ID" || fail "agent missing after recreate"
echo "$STATUS" | grep -q revoked || fail "revoked status not preserved"

echo "==> SQLite integrity_check"
docker run --rm -v "${VOLUME}:/state" --entrypoint python "$IMAGE" -c \
  'import sqlite3; r=sqlite3.connect("/state/controller.sqlite3").execute("PRAGMA integrity_check").fetchone(); assert r and r[0]=="ok", r; print("ok")' \
  >"$LOG_DIR/sqlite.txt" 2>&1 || fail "sqlite integrity failed"

echo "==> Key permissions"
docker run --rm -v "${VOLUME}:/state" --entrypoint python "$IMAGE" -c \
  'import os; assert (os.stat("/state/ca/ca.key").st_mode & 0o777)==0o600; assert (os.stat("/state/server/server.key").st_mode & 0o777)==0o600; print("ok")' \
  || fail "key permissions not 0600"

echo "==> Unclean kill + restart"
docker kill "$NAME2" >/dev/null || fail "kill failed"
docker rm -f "$NAME2" >/dev/null
CONTAINERS=("${CONTAINERS[@]/$NAME2}")
NAME3="bl-ctrl-harness3-${SUFFIX}"
run_hardened "$NAME3"
wait_healthy "$NAME3" || fail "post-kill health failed"
STATUS2="$(docker run --rm -v "${VOLUME}:/state" --entrypoint backuplint "$IMAGE" \
  controller agents --data-dir /state)"
echo "$STATUS2" | grep -q "$AGENT_ID" || fail "agent missing after kill/restart"

echo "==> Wrong CA rejection"
OTHER="$LOG_DIR/other-controller"
mkdir -p "$OTHER"
PYTHONPATH=src "$PYTHON" - <<PY
from pathlib import Path
from backuplint.fleet.controller import FleetController
c = FleetController(Path("$OTHER"), hostname="localhost")
c.close()
PY
TOKEN2="$(docker run --rm -v "${VOLUME}:/state" --entrypoint backuplint "$IMAGE" \
  controller enroll-token --data-dir /state --label badca)"
PORT3="$(host_port "$NAME3")"
if PYTHONPATH=src "$PYTHON" -m backuplint agent enroll \
  --controller "https://127.0.0.1:${PORT3}" \
  --token "$TOKEN2" \
  --ca-cert "$OTHER/ca/ca.crt" \
  --identity-dir "$LOG_DIR/agent-bad" >"$LOG_DIR/bad-enroll.out" 2>"$LOG_DIR/bad-enroll.err"; then
  fail "enroll with wrong CA should fail"
fi

echo "==> Ownership failure (non-writable state)"
OWN="bl-ctrl-ownfail-${SUFFIX}"
mkdir -p "$LOG_DIR/root-state"
# Host dir owned by root is not creatable without sudo; simulate by chmod a-w as current user after creating.
chmod 000 "$LOG_DIR/root-state" || true
if docker run --rm --name "$OWN" \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:size=8m,mode=1777 \
  -e BACKUPLINT_CONTROLLER_HOSTNAME=localhost \
  -v "$LOG_DIR/root-state:/state:ro" \
  "$IMAGE" >"$LOG_DIR/ownfail.out" 2>"$LOG_DIR/ownfail.err"; then
  chmod 700 "$LOG_DIR/root-state" || true
  fail "controller should fail on read-only state mount"
fi
chmod 700 "$LOG_DIR/root-state" || true
grep -qi 'ERROR\|not writable\|Read-only\|Permission\|OSError\|cannot' "$LOG_DIR/ownfail.err" "$LOG_DIR/ownfail.out" \
  || fail "expected clear permission error on bad state mount"

echo "HARNESS OK"
exit 0
