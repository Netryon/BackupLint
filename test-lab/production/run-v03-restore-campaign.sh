#!/usr/bin/env bash
# Disposable Restic restore-verification campaign for BackupLint v0.3.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate" 2>/dev/null || true

BASE="$(mktemp -d /tmp/backuplint-v03-restore-XXXXXX)"
PASSFILE="$BASE/restic.pass"
REPO="$BASE/repo"
DATA="$BASE/data"
COMPOSE="$BASE/compose.yml"
CFG="$BASE/backuplint.yml"
export RESTIC_PASSWORD_FILE="$PASSFILE"
printf 'v03-restore-campaign-pass\n' >"$PASSFILE"
chmod 600 "$PASSFILE"

mkdir -p "$DATA"
echo 'payload' >"$DATA/file.txt"
mkdir -p "$DATA/nested"
echo 'nested' >"$DATA/nested/x.txt"

restic init -r "$REPO" >/dev/null
restic -r "$REPO" backup "$DATA" >/dev/null

cat >"$COMPOSE" <<EOF
services:
  app:
    image: alpine:3.20
    command: ["sleep", "180"]
    volumes:
      - $DATA:/data
EOF

cat >"$CFG" <<EOF
backup_paths: []
restic:
  repository: $REPO
  password_file: $PASSFILE
  restore_verification:
    mode: selected
EOF

PASS=0
FAIL=0
ok() { echo "OK $*"; PASS=$((PASS + 1)); }
bad() { echo "FAIL $*"; FAIL=$((FAIL + 1)); }

cleanup() {
  docker compose -f "$COMPOSE" down >/dev/null 2>&1 || true
  rm -rf "$BASE"
}
trap cleanup EXIT

docker compose -f "$COMPOSE" up -d >/dev/null

echo "=== healthy selected restore ==="
set +e
OUT=$(backuplint scan "$COMPOSE" --config "$CFG" 2>&1)
EC=$?
set -e
echo "$OUT" | grep -q 'selected-path restore verification passed' && ok healthy_selected || bad healthy_selected
[[ "$EC" -eq 0 ]] && ok healthy_exit || bad "healthy_exit=$EC"

echo "=== CLI full override ==="
set +e
OUT=$(backuplint scan "$COMPOSE" --config "$CFG" --restore-verify full 2>&1)
EC=$?
set -e
echo "$OUT" | grep -q 'full-path restore verification passed' && ok full_override || bad full_override
[[ "$EC" -eq 0 ]] && ok full_exit || bad "full_exit=$EC"

echo "=== wrong password ==="
printf 'wrong\n' >"$BASE/bad.pass"
chmod 600 "$BASE/bad.pass"
cat >"$BASE/bad.yml" <<EOF
backup_paths: []
restic:
  repository: $REPO
  password_file: $BASE/bad.pass
  restore_verification:
    mode: selected
EOF
set +e
OUT=$(backuplint scan "$COMPOSE" --config "$BASE/bad.yml" 2>&1)
EC=$?
set -e
[[ "$EC" -eq 2 ]] && ok bad_password_exit || bad "bad_password_exit=$EC"
echo "$OUT" | grep -qi 'authentication failed' && ok bad_password_msg || bad bad_password_msg

echo "=== missing repository ==="
cat >"$BASE/miss.yml" <<EOF
backup_paths: []
restic:
  repository: $BASE/does-not-exist
  password_file: $PASSFILE
  restore_verification:
    mode: selected
EOF
set +e
OUT=$(backuplint scan "$COMPOSE" --config "$BASE/miss.yml" 2>&1)
EC=$?
set -e
[[ "$EC" -eq 2 ]] && ok missing_repo_exit || bad "missing_repo_exit=$EC"

echo "=== JSON contract ==="
set +e
JSON=$(backuplint scan "$COMPOSE" --config "$CFG" --json 2>&1)
EC=$?
set -e
[[ "$EC" -eq 0 ]] && ok json_exit || bad "json_exit=$EC"
printf '%s' "$JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin); r=d["restore_verification"]; assert r["status"]=="passed"; assert r["mode"]=="selected"; assert "snapshot_id" in r' \
  && ok json_contract || bad json_contract

echo "=== reliability x5 ==="
for i in 1 2 3 4 5; do
  set +e
  OUT=$(backuplint scan "$COMPOSE" --config "$CFG" 2>&1)
  EC=$?
  set -e
  [[ "$EC" -eq 0 ]] && echo "$OUT" | grep -q 'selected-path restore verification passed' && ok "repeat_$i" || bad "repeat_$i"
done

echo "=== summary PASS=$PASS FAIL=$FAIL ==="
if [[ "$FAIL" -eq 0 ]]; then
  echo V03_RESTORE_CAMPAIGN_PASSED
  exit 0
fi
echo V03_RESTORE_CAMPAIGN_FAILED
exit 1
