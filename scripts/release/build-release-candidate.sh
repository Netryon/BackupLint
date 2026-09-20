#!/usr/bin/env bash
# BackupLint release-candidate entrypoint.
# Builds artifacts, runs gates, optional second build for reproducibility,
# and writes evidence under dist-release/ (or --outdir).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  else
    PYTHON=python3
  fi
fi

OUTDIR="${OUTDIR:-$ROOT/dist-release}"
FORCE=0
ALLOW_DIRTY=0
SKIP_CONTAINER=0
SKIP_HARNESS=0
SKIP_INTEGRATION=0
COMPARE=1

usage() {
  cat <<'EOF'
Usage: scripts/release/build-release-candidate.sh [options]

Options:
  --outdir DIR          Output directory (default: ./dist-release)
  --force               Overwrite non-empty outdir
  --allow-dirty         Allow dirty git worktree
  --skip-container      Skip controller image build/SBOM/harness
  --skip-harness        Skip controller hardening harness
  --skip-integration    Skip controller container integration tests
  --no-compare          Skip second clean build reproducibility compare
  -h, --help            Show help

Never publishes to PyPI, never pushes images, never creates GitHub Releases/tags.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --outdir) OUTDIR="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --allow-dirty) ALLOW_DIRTY=1; shift ;;
    --skip-container) SKIP_CONTAINER=1; shift ;;
    --skip-harness) SKIP_HARNESS=1; shift ;;
    --skip-integration) SKIP_INTEGRATION=1; shift ;;
    --no-compare) COMPARE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

GATE_ARGS=(--outdir "$OUTDIR")
[[ "$FORCE" -eq 1 ]] && GATE_ARGS+=(--force)
[[ "$ALLOW_DIRTY" -eq 1 ]] && GATE_ARGS+=(--allow-dirty)
[[ "$SKIP_CONTAINER" -eq 1 ]] && GATE_ARGS+=(--skip-container)
[[ "$SKIP_HARNESS" -eq 1 ]] && GATE_ARGS+=(--skip-harness)
[[ "$SKIP_INTEGRATION" -eq 1 ]] && GATE_ARGS+=(--skip-integration)

echo "==> Running release gates into $OUTDIR"
"$PYTHON" "$ROOT/scripts/release/run_gates.py" "${GATE_ARGS[@]}"

if [[ "$COMPARE" -eq 1 ]]; then
  SECOND="${OUTDIR%/}-rebuild"
  echo "==> Second clean build for reproducibility compare -> $SECOND"
  BUILD_ARGS=(--outdir "$SECOND" --force)
  [[ "$ALLOW_DIRTY" -eq 1 ]] && BUILD_ARGS+=(--allow-dirty)
  [[ "$SKIP_CONTAINER" -eq 1 ]] && BUILD_ARGS+=(--skip-container)
  "$PYTHON" "$ROOT/scripts/release/build_rc.py" "${BUILD_ARGS[@]}"
  mkdir -p "$OUTDIR/evidence"
  "$PYTHON" "$ROOT/scripts/release/compare_builds.py" \
    --a "$OUTDIR" \
    --b "$SECOND" \
    --report "$OUTDIR/evidence/reproducibility.json"
fi

echo "Release candidate workflow complete."
echo "Evidence: $OUTDIR/evidence/release-evidence.json"
echo "Checksums: $OUTDIR/meta/checksums.json"
echo "DO NOT publish without explicit owner approval."
