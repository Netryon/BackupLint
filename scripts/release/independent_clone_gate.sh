#!/usr/bin/env bash
# Independent-clone release gate: fresh git worktree, no workshop dirt.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SHA="$(git -C "$ROOT" rev-parse HEAD)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/bl-independent-clone.XXXXXX")"
CLONE="$TMP/backuplint"
OUT="$TMP/dist-release"
KEEP=0

cleanup() {
  git -C "$ROOT" worktree remove --force "$CLONE" >/dev/null 2>&1 || true
  if [[ "$KEEP" -eq 0 ]]; then
    rm -rf "$TMP"
  else
    echo "Preserved independent-clone workspace: $TMP" >&2
  fi
}
trap cleanup EXIT

fail() {
  KEEP=1
  echo "INDEPENDENT CLONE GATE FAIL: $*" >&2
  exit 1
}

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  else
    PYTHON=python3
  fi
fi

echo "==> Creating detached worktree at $SHA"
git -C "$ROOT" worktree add --detach "$CLONE" "$SHA" >/dev/null
chmod 755 "$CLONE"/scripts/release/*.sh "$CLONE"/docker/controller-*.sh \
  "$CLONE"/docker/controller-*.py "$CLONE"/deploy/controller/run-hardening-harness.sh 2>/dev/null || true

# Install build tooling into a dedicated venv (not the developer editable env).
VENV="$TMP/venv"
"$PYTHON" -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip build pip-audit ruff bandit pytest hypothesis >/dev/null
"$VENV/bin/pip" install -e "$CLONE[dev]" >/dev/null

echo "==> Running release candidate workflow in independent worktree"
SKIP_HARNESS_FLAG=()
if [[ "${SKIP_HARNESS:-0}" == "1" ]]; then
  SKIP_HARNESS_FLAG+=(--skip-harness)
fi
(
  cd "$CLONE"
  PYTHON="$VENV/bin/python" ./scripts/release/build-release-candidate.sh \
    --outdir "$OUT" \
    --force \
    --no-compare \
    "${SKIP_HARNESS_FLAG[@]}"
) || fail "release workflow failed in independent clone"

[[ -f "$OUT/meta/checksums.json" ]] || fail "missing checksums"
[[ -f "$OUT/evidence/release-evidence.json" ]] || fail "missing evidence"
[[ -f "$OUT/evidence/clean-room-install.json" ]] || fail "missing clean-room report"

WHEEL="$(ls "$OUT"/python/*.whl | head -1)"
"$VENV/bin/pip" install "$WHEEL" >/dev/null
"$VENV/bin/backuplint" --version | grep -q '.' || fail "installed CLI version failed"

echo "INDEPENDENT CLONE GATE OK"
echo "Artifacts at: $OUT"
exit 0
