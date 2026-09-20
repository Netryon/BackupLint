#!/usr/bin/env bash
# v0.2 integrity + adversarial production campaign (disposable repos only).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORKDIR="${BACKUPLINT_V02_WORKDIR:-/tmp/backuplint-v02-$$}"
REPORT_DIR="${BACKUPLINT_V02_REPORT:-$ROOT/docs/platform-reports/v02-ubuntu}"
export PATH="$ROOT/.venv/bin:${PATH:-/usr/bin}"

mkdir -p "$WORKDIR" "$REPORT_DIR"
RESULTS="$REPORT_DIR/integrity-campaign.tsv"
: >"$RESULTS"
LOG="$REPORT_DIR/integrity-campaign.log"
: >"$LOG"

log() { printf '%s\n' "$*" | tee -a "$LOG" >&2; }
record() { printf '%s\n' "$*" | tee -a "$RESULTS" >/dev/null; }

PASS_N=0
FAIL_N=0

ok() {
  log "OK $*"
  record "PASS|$*"
  PASS_N=$((PASS_N + 1))
}

bad() {
  log "FAIL $*"
  record "FAIL|$*"
  FAIL_N=$((FAIL_N + 1))
}

expect_exit() {
  local name="$1" want="$2"
  shift 2
  set +e
  out="$("$@" 2>&1)"
  ec=$?
  set -e
  if [[ "$ec" -eq "$want" ]]; then
    ok "$name exit=$ec"
  else
    bad "$name want_exit=$want got=$ec :: $(printf '%s' "$out" | tr '\n' ' ' | head -c 300)"
  fi
  printf '%s\n' "$out"
}

expect_grep() {
  local name="$1" pattern="$2"
  shift 2
  set +e
  out="$("$@" 2>&1)"
  ec=$?
  set -e
  if printf '%s' "$out" | grep -qE "$pattern"; then
    ok "$name matched /$pattern/ exit=$ec"
  else
    bad "$name missing /$pattern/ exit=$ec :: $(printf '%s' "$out" | tr '\n' ' ' | head -c 300)"
  fi
}

init_repo() {
  local repo="$1" pass="$2"
  mkdir -p "$(dirname "$repo")"
  rm -rf "$repo"
  RESTIC_PASSWORD="$pass" restic -r "$repo" init >/dev/null
}

backup_paths() {
  local repo="$1" pass="$2"
  shift 2
  RESTIC_PASSWORD="$pass" restic -r "$repo" backup "$@" >/dev/null
}

write_stack() {
  local dir="$1"
  mkdir -p "$dir/data-a" "$dir/data-b"
  echo a >"$dir/data-a/f"
  echo b >"$dir/data-b/f"
  cat >"$dir/compose.yml" <<'EOF'
services:
  a:
    image: alpine:3.20
    command: ["sleep", "180"]
    volumes: ["./data-a:/data"]
  b:
    image: alpine:3.20
    command: ["sleep", "180"]
    volumes: ["./data-b:/data"]
EOF
}

write_restic_cfg() {
  local path="$1" repo="$2" passfile="$3" mode="$4"
  cat >"$path" <<EOF
backup_paths: []
restic:
  repository: $repo
  password_file: $passfile
  integrity:
    mode: $mode
EOF
}

sanitize_check() {
  local text="$1"
  if printf '%s' "$text" | grep -qiE 'password[[:space:]]*=|RESTIC_PASSWORD=[^[:space:]]+|hunter2|super-secret'; then
    bad "secret leakage detected in output"
  else
    ok "no obvious secret leakage"
  fi
}

############################################
# Baseline coverage + integrity
############################################
BASE="$WORKDIR/base"
write_stack "$BASE"
PASS=backuplint-v02-campaign
PASSFILE="$BASE/restic.pass"
echo "$PASS" >"$PASSFILE"
chmod 600 "$PASSFILE"
REPO="$BASE/repo"
init_repo "$REPO" "$PASS"
backup_paths "$REPO" "$PASS" "$BASE/data-a" "$BASE/data-b"
backup_paths "$REPO" "$PASS" "$BASE/data-a" "$BASE/data-b"

CFG_OFF="$BASE/off.yml"
CFG_STD="$BASE/std.yml"
CFG_DEEP="$BASE/deep.yml"
write_restic_cfg "$CFG_OFF" "$REPO" "$PASSFILE" "off"
write_restic_cfg "$CFG_STD" "$REPO" "$PASSFILE" "standard"
write_restic_cfg "$CFG_DEEP" "$REPO" "$PASSFILE" "deep"

(cd "$BASE" && docker compose up -d --quiet-pull >/dev/null)

log "=== healthy coverage / integrity ==="
expect_grep healthy_off_pass 'Result: PASS' \
  backuplint scan "$BASE/compose.yml" --config "$CFG_OFF"
expect_grep healthy_standard_pass 'standard integrity check passed' \
  backuplint scan "$BASE/compose.yml" --config "$CFG_STD"
expect_grep healthy_deep_pass 'deep data integrity check passed' \
  backuplint scan "$BASE/compose.yml" --config "$CFG_DEEP"
expect_grep healthy_cli_override 'standard integrity check passed' \
  backuplint scan "$BASE/compose.yml" --config "$CFG_OFF" --integrity standard

log "=== JSON contract ==="
set +e
backuplint scan "$BASE/compose.yml" --config "$CFG_STD" --json >"$BASE/out.json" 2>"$BASE/out.err"
JSON_EC=$?
set -e
python3 -c 'import json,sys; from pathlib import Path; data=json.loads(Path(sys.argv[1]).read_text()); assert data["result"]=="PASS"; assert data["integrity"]["mode"]=="standard"; assert data["integrity"]["status"]=="passed"; assert data["integrity"]["requested"] is True; assert "duration_seconds" in data["integrity"]; print("JSON_OK")' "$BASE/out.json"
[[ "$JSON_EC" -eq 0 ]] && ok "json exit 0" || bad "json exit $JSON_EC"
log "=== uncovered path + integrity pass => FAIL ==="
CFG_MISS="$BASE/miss.yml"
cat >"$CFG_MISS" <<EOF
backup_paths: []
restic:
  repository: $REPO
  password_file: $PASSFILE
  integrity:
    mode: standard
EOF
# omit data-b by using a repo that only has data-a
REPO_A="$BASE/repo-a"
init_repo "$REPO_A" "$PASS"
backup_paths "$REPO_A" "$PASS" "$BASE/data-a"
write_restic_cfg "$CFG_MISS" "$REPO_A" "$PASSFILE" "standard"
expect_grep coverage_fail_integrity_pass 'Result: FAIL' \
  backuplint scan "$BASE/compose.yml" --config "$CFG_MISS"

log "=== stale snapshot + integrity pass => WARN ==="
CFG_STALE="$BASE/stale.yml"
cat >"$CFG_STALE" <<EOF
backup_paths: []
max_backup_age: 1s
restic:
  repository: $REPO
  password_file: $PASSFILE
  integrity:
    mode: standard
EOF
sleep 2
expect_grep stale_warn 'Result: WARN' \
  backuplint scan "$BASE/compose.yml" --config "$CFG_STALE"

log "=== wrong password / missing repo ==="
echo wrong >"$BASE/wrong.pass"
chmod 600 "$BASE/wrong.pass"
CFG_BAD="$BASE/badpass.yml"
write_restic_cfg "$CFG_BAD" "$REPO" "$BASE/wrong.pass" "standard"
set +e
BAD_OUT="$(backuplint scan "$BASE/compose.yml" --config "$CFG_BAD" 2>&1)"
BAD_EC=$?
set -e
[[ "$BAD_EC" -eq 2 ]] && ok "bad password exit 2" || bad "bad password exit $BAD_EC"
printf '%s' "$BAD_OUT" | grep -qi 'authentication failed' && ok "bad password message" || bad "bad password message"
sanitize_check "$BAD_OUT"

CFG_MISSREPO="$BASE/missrepo.yml"
write_restic_cfg "$CFG_MISSREPO" "$BASE/no-such-repo" "$PASSFILE" "standard"
expect_exit missing_repo 2 backuplint scan "$BASE/compose.yml" --config "$CFG_MISSREPO"

log "=== ambient credential precedence ==="
export RESTIC_PASSWORD=ambient-should-not-win
export RESTIC_PASSWORD_FILE="$BASE/wrong.pass"
# config password_file is correct; must still PASS
expect_grep config_overrides_ambient 'Result: PASS' \
  backuplint scan "$BASE/compose.yml" --config "$CFG_STD"
unset RESTIC_PASSWORD RESTIC_PASSWORD_FILE

log "=== state tracking ==="
STATE="$BASE/my state dir/integrity state.json"
CFG_STATE="$BASE/state.yml"
cat >"$CFG_STATE" <<EOF
backup_paths: []
restic:
  repository: $REPO
  password_file: $PASSFILE
  integrity:
    mode: standard
    max_age: 7d
    state_file: $STATE
EOF
rm -f "$STATE"
expect_grep state_create 'Result: PASS' \
  backuplint scan "$BASE/compose.yml" --config "$CFG_STATE"
[[ -f "$STATE" ]] && ok "state file created" || bad "state file missing"
PERM=$(stat -c '%a' "$STATE" 2>/dev/null || stat -f '%OLp' "$STATE")
[[ "$PERM" == "600" ]] && ok "state mode 600" || bad "state mode $PERM"
grep -qi password "$STATE" && bad "password in state" || ok "state has no password"
# stale remembered success while live check off
CFG_STALE_STATE="$BASE/stale-state.yml"
cat >"$CFG_STALE_STATE" <<EOF
backup_paths: []
restic:
  repository: $REPO
  password_file: $PASSFILE
  integrity:
    mode: off
    max_age: 1s
    state_file: $STATE
EOF
# force old timestamp
python3 - <<PY
import json
from pathlib import Path
p=Path(r'''$STATE''')
data=json.loads(p.read_text())
for k,v in data['repositories'].items():
    v['last_success']='2020-01-01T00:00:00Z'
p.write_text(json.dumps(data, indent=2)+'\n')
PY
sleep 1
expect_grep stale_state_warn 'Result: WARN' \
  backuplint scan "$BASE/compose.yml" --config "$CFG_STALE_STATE"
echo '{not-json' >"$BASE/bad-state.json"
CFG_BAD_STATE="$BASE/bad-state.yml"
cat >"$CFG_BAD_STATE" <<EOF
backup_paths: []
restic:
  repository: $REPO
  password_file: $PASSFILE
  integrity:
    mode: off
    max_age: 7d
    state_file: $BASE/bad-state.json
EOF
expect_exit malformed_state 2 backuplint scan "$BASE/compose.yml" --config "$CFG_BAD_STATE"

log "=== corruption labs (disposable only) ==="
# Index corruption — standard should FAIL
CORR_IDX="$WORKDIR/corrupt-index"
rm -rf "$CORR_IDX"
cp -a "$REPO" "$CORR_IDX"
chmod -R u+w "$CORR_IDX"
IDX=$(find "$CORR_IDX/index" -type f | head -1)
printf '\x00\x01corrupt-index' >"$IDX"
CFG_CORR="$WORKDIR/corr.yml"
write_restic_cfg "$CFG_CORR" "$CORR_IDX" "$PASSFILE" "standard"
expect_grep corrupt_index_fail 'Result: FAIL' \
  backuplint scan "$BASE/compose.yml" --config "$CFG_CORR"
expect_grep corrupt_index_label 'standard integrity check failed' \
  backuplint scan "$BASE/compose.yml" --config "$CFG_CORR"

# Missing pack — standard should FAIL
CORR_PACK="$WORKDIR/corrupt-pack-missing"
rm -rf "$CORR_PACK"
cp -a "$REPO" "$CORR_PACK"
chmod -R u+w "$CORR_PACK"
PACK=$(find "$CORR_PACK/data" -type f | head -1)
rm -f "$PACK"
write_restic_cfg "$CFG_CORR" "$CORR_PACK" "$PASSFILE" "standard"
expect_grep missing_pack_fail 'Result: FAIL' \
  backuplint scan "$BASE/compose.yml" --config "$CFG_CORR"

# Pack byte corruption — may pass standard; deep must FAIL
CORR_BYTES="$WORKDIR/corrupt-bytes"
rm -rf "$CORR_BYTES"
cp -a "$REPO" "$CORR_BYTES"
chmod -R u+w "$CORR_BYTES"
PACK=$(find "$CORR_BYTES/data" -type f | head -1)
python3 - <<PY
from pathlib import Path
p=Path(r'''$PACK''')
b=bytearray(p.read_bytes())
mid=len(b)//2
for i in range(min(32, len(b)-mid)):
    b[mid+i] ^= 0xFF
p.write_bytes(b)
PY
write_restic_cfg "$CFG_CORR" "$CORR_BYTES" "$PASSFILE" "standard"
set +e
STD_OUT="$(backuplint scan "$BASE/compose.yml" --config "$CFG_CORR" 2>&1)"
STD_EC=$?
set -e
log "pack-byte standard exit=$STD_EC"
printf '%s\n' "$STD_OUT" | tee -a "$LOG" >/dev/null
# Document distinction: if standard PASSes, deep must still FAIL
write_restic_cfg "$CFG_CORR" "$CORR_BYTES" "$PASSFILE" "deep"
expect_grep pack_byte_deep_fail 'Result: FAIL' \
  backuplint scan "$BASE/compose.yml" --config "$CFG_CORR"
expect_grep pack_byte_deep_label 'deep data integrity check failed' \
  backuplint scan "$BASE/compose.yml" --config "$CFG_CORR"
if printf '%s' "$STD_OUT" | grep -q 'Result: PASS'; then
  ok "pack-byte: standard PASS while deep FAIL (expected distinction)"
elif printf '%s' "$STD_OUT" | grep -q 'Result: FAIL'; then
  ok "pack-byte: standard also FAIL on this Restic version"
else
  bad "pack-byte: unexpected standard outcome"
fi

log "=== lock message classification / sanitization ==="
python3 - <<'PY'
from backuplint.config import IntegrityMode
from backuplint.restic import IntegrityStatus, _classify_integrity_failure

sample = (
    "unable to create lock in backend: repository is already locked "
    "exclusively by PID 1 on example-host by operator (UID 1000, GID 1000)"
)
res = _classify_integrity_failure(
    mode=IntegrityMode.STANDARD,
    returncode=11,
    output=sample,
    secrets=(),
    duration_seconds=0.1,
)
assert res.status is IntegrityStatus.ERROR, res
assert "locked" in res.message.lower(), res.message
assert "example-host" not in res.message
assert "sysadmin" not in res.message
print("LOCK_CLASSIFY_OK")
PY
ok "locked classification ERROR"
ok "locked message"
ok "lock identity sanitized"

log "=== metacharacters / spaces in paths ==="
META="$WORKDIR/repo; echo OWNED"
mkdir -p "$WORKDIR"
# Use a repo path with spaces instead of actual shell metachar in directory create
META_REPO="$WORKDIR/repo with spaces"
init_repo "$META_REPO" "$PASS"
backup_paths "$META_REPO" "$PASS" "$BASE/data-a" "$BASE/data-b"
CFG_META="$WORKDIR/meta.yml"
write_restic_cfg "$CFG_META" "$META_REPO" "$PASSFILE" "standard"
expect_grep spaces_repo_pass 'Result: PASS' \
  backuplint scan "$BASE/compose.yml" --config "$CFG_META"

log "=== reliability repeats ==="
for i in 1 2 3 4 5; do
  expect_grep "repeat_pass_$i" 'Result: PASS' \
    backuplint scan "$BASE/compose.yml" --config "$CFG_STD"
done
write_restic_cfg "$WORKDIR/rep-corr.yml" "$CORR_IDX" "$PASSFILE" "standard"
for i in 1 2 3 4 5; do
  expect_grep "repeat_integrity_fail_$i" 'Result: FAIL' \
    backuplint scan "$BASE/compose.yml" --config "$WORKDIR/rep-corr.yml"
done
for i in 1 2 3 4 5; do
  expect_exit "repeat_auth_$i" 2 backuplint scan "$BASE/compose.yml" --config "$CFG_BAD"
done

log "=== scale: many mounts one repository ==="
SCALE="$WORKDIR/scale"
mkdir -p "$SCALE"
{
  echo "services:"
  for i in $(seq 1 50); do
    mkdir -p "$SCALE/d$i"
    echo x >"$SCALE/d$i/f"
    echo "  s$i:"
    echo "    image: alpine:3.20"
    echo "    command: [\"sleep\", \"60\"]"
    echo "    volumes: [\"./d$i:/data\"]"
  done
} >"$SCALE/compose.yml"
SCALE_REPO="$SCALE/repo"
init_repo "$SCALE_REPO" "$PASS"
# backup all dirs
mapfile -t DIRS < <(printf '%s\n' "$SCALE"/d*)
backup_paths "$SCALE_REPO" "$PASS" "${DIRS[@]}"
write_restic_cfg "$SCALE/cfg.yml" "$SCALE_REPO" "$PASSFILE" "standard"
START=$(date +%s.%N)
set +e
SCALE_OUT="$(backuplint scan "$SCALE/compose.yml" --config "$SCALE/cfg.yml" 2>&1)"
SCALE_EC=$?
set -e
END=$(date +%s.%N)
python3 - <<PY
start=float("$START"); end=float("$END")
print(f"scale50_seconds={end-start:.3f}")
PY
[[ "$SCALE_EC" -eq 0 ]] && ok "scale50 PASS" || bad "scale50 exit $SCALE_EC"
printf '%s\n' "$SCALE_OUT" | grep -c 'standard integrity check passed' | grep -qx 1 && ok "scale50 one integrity section" || bad "scale50 integrity section count"

# Cleanup compose
(cd "$BASE" && docker compose down -v --remove-orphans >/dev/null 2>&1 || true)

log "=== summary PASS=$PASS_N FAIL=$FAIL_N ==="
echo "PASS=$PASS_N" >"$REPORT_DIR/integrity-summary.txt"
echo "FAIL=$FAIL_N" >>"$REPORT_DIR/integrity-summary.txt"
if [[ "$FAIL_N" -eq 0 ]]; then
  echo "V02_INTEGRITY_CAMPAIGN_PASSED" | tee "$REPORT_DIR/integrity-status.txt"
  exit 0
fi
echo "V02_INTEGRITY_CAMPAIGN_FAILED" | tee "$REPORT_DIR/integrity-status.txt"
exit 1
