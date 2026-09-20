#!/usr/bin/env bash
# Run BackupLint compatibility validation inside a guest VM.
# Expects the repository at ~/backuplint (or BACKUPLINT_ROOT).
set -euo pipefail

ROOT="${BACKUPLINT_ROOT:-$HOME/backuplint}"
REPORT_DIR="${BACKUPLINT_COMPAT_REPORT:-$HOME/backuplint-compat-report}"
mkdir -p "$REPORT_DIR"

log() { printf '%s\n' "$*" | tee -a "$REPORT_DIR/run.log" >&2; }
fail() { log "FAIL: $*"; exit 1; }

cd "$ROOT"
[[ -d src/backuplint ]] || fail "BackupLint source not found at $ROOT"

bash "$ROOT/scripts/platform-inventory.sh" | tee "$REPORT_DIR/inventory.txt"

# Prefer a newer interpreter when the distro default is too old (e.g. Rocky 9).
PY=python3
if command -v python3.12 >/dev/null 2>&1; then
  PY=python3.12
elif command -v python3.11 >/dev/null 2>&1; then
  PY=python3.11
fi
"$PY" --version | tee -a "$REPORT_DIR/inventory.txt"

"$PY" -m venv "$ROOT/.venv"
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
# Debian/cloud images ship an old system setuptools that can leak into the
# venv; upgrade tooling so pip-audit matches the Ubuntu/CI baseline.
python -m pip install -q --upgrade pip setuptools wheel
pip install -q -e ".[dev]"

backuplint --help >/dev/null || fail "help failed"
backuplint --version | tee "$REPORT_DIR/version.txt"

{ time -p backuplint --help >/dev/null; } 2>"$REPORT_DIR/time-help.txt" || true
{ time -p backuplint --version >/dev/null; } 2>"$REPORT_DIR/time-version.txt" || true

ruff check src tests | tee "$REPORT_DIR/ruff.txt"
bandit -r src -q | tee "$REPORT_DIR/bandit.txt"
pip-audit 2>&1 | tee "$REPORT_DIR/pip-audit.txt"

pytest -q tests/unit | tee "$REPORT_DIR/pytest-unit.txt"

cat >"$REPORT_DIR/run-docker.sh" <<'EOS'
#!/usr/bin/env bash
set -euo pipefail
ROOT="${BACKUPLINT_ROOT:-$HOME/backuplint}"
REPORT_DIR="${BACKUPLINT_COMPAT_REPORT:-$HOME/backuplint-compat-report}"
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
cd "$ROOT"
pytest -q tests/integration | tee "$REPORT_DIR/pytest-integration.txt"
./test-lab/run-milestone6.sh | tee "$REPORT_DIR/milestone6.txt"
# Harness progress goes to stderr; merge so the report file is complete.
./test-lab/production/run-production-validation.sh 2>&1 | tee "$REPORT_DIR/production-harness.txt"
./test-lab/production/run-v02-integrity-campaign.sh 2>&1 | tee "$REPORT_DIR/v02-integrity-campaign.txt"
./test-lab/production/run-v03-restore-campaign.sh 2>&1 | tee "$REPORT_DIR/v03-restore-campaign.txt"
mkdir -p /tmp/bl-rel/data
printf 'services:\n  a:\n    image: alpine:3.20\n    command: ["true"]\n    volumes: ["./data:/data"]\n' >/tmp/bl-rel/compose.yml
echo x >/tmp/bl-rel/data/f
echo 'backup_paths: ["/tmp/nope"]' >/tmp/bl-rel/backuplint.yml
for i in 1 2 3 4 5; do
  backuplint scan /tmp/bl-rel/compose.yml --config /tmp/bl-rel/backuplint.yml >/tmp/bl-rel/out-$i.txt || true
done
cmp /tmp/bl-rel/out-1.txt /tmp/bl-rel/out-5.txt
echo RELIABLE_OK | tee -a "$REPORT_DIR/run.log"
EOS
chmod +x "$REPORT_DIR/run-docker.sh"

if getent group docker >/dev/null && command -v sg >/dev/null; then
  sg docker -c "BACKUPLINT_ROOT=$ROOT BACKUPLINT_COMPAT_REPORT=$REPORT_DIR $REPORT_DIR/run-docker.sh"
else
  BACKUPLINT_ROOT="$ROOT" BACKUPLINT_COMPAT_REPORT="$REPORT_DIR" "$REPORT_DIR/run-docker.sh"
fi

if grep -q 'ALL RECORDED SCENARIOS PASSED' "$REPORT_DIR/production-harness.txt"; then
  echo HARNESS_PASS | tee "$REPORT_DIR/harness-status.txt"
else
  echo HARNESS_FAIL | tee "$REPORT_DIR/harness-status.txt"
  fail "production harness did not pass"
fi

if grep -q 'V02_INTEGRITY_CAMPAIGN_PASSED' "$REPORT_DIR/v02-integrity-campaign.txt"; then
  echo V02_INTEGRITY_PASS | tee "$REPORT_DIR/v02-integrity-status.txt"
else
  echo V02_INTEGRITY_FAIL | tee "$REPORT_DIR/v02-integrity-status.txt"
  fail "v0.2 integrity campaign did not pass"
fi

if grep -q 'V03_RESTORE_CAMPAIGN_PASSED' "$REPORT_DIR/v03-restore-campaign.txt"; then
  echo V03_RESTORE_PASS | tee "$REPORT_DIR/v03-restore-status.txt"
else
  echo V03_RESTORE_FAIL | tee "$REPORT_DIR/v03-restore-status.txt"
  fail "v0.3 restore campaign did not pass"
fi

log "COMPAT_OK"
