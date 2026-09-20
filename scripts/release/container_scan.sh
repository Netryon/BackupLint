#!/usr/bin/env bash
# Local/CI container + filesystem vulnerability scan path for BackupLint.
# Does NOT install scanners into production hosts. Workshop/CI only.
#
# Usage:
#   scripts/release/container_scan.sh --image backuplint-controller:rc --outdir /tmp/bl-scan
#   scripts/release/container_scan.sh --fs /path/to/export --outdir /tmp/bl-scan
#
# Preferred scanner: Trivy. Fallback: Grype.
# Exit codes:
#   0 = scan completed under threshold
#   2 = findings at/above threshold
#   3 = scanner unavailable (pending)
#   4 = usage/prereq error
set -euo pipefail

IMAGE=""
FS_PATH=""
OUTDIR=""
SEVERITY="${BACKUPLINT_SCAN_SEVERITY:-HIGH,CRITICAL}"
SCANNER_PREF="${BACKUPLINT_SCANNER:-auto}"

usage() {
  sed -n '2,20p' "$0"
  exit 4
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image) IMAGE="$2"; shift 2 ;;
    --fs) FS_PATH="$2"; shift 2 ;;
    --outdir) OUTDIR="$2"; shift 2 ;;
    --severity) SEVERITY="$2"; shift 2 ;;
    --scanner) SCANNER_PREF="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $1" >&2; usage ;;
  esac
done

if [[ -z "$OUTDIR" ]]; then
  echo "--outdir is required" >&2
  exit 4
fi
if [[ -z "$IMAGE" && -z "$FS_PATH" ]]; then
  echo "provide --image and/or --fs" >&2
  exit 4
fi
mkdir -p "$OUTDIR"

pick_scanner() {
  case "$SCANNER_PREF" in
    trivy)
      command -v trivy >/dev/null || { echo "trivy not found" >&2; return 1; }
      echo trivy
      ;;
    grype)
      command -v grype >/dev/null || { echo "grype not found" >&2; return 1; }
      echo grype
      ;;
    auto)
      if command -v trivy >/dev/null; then echo trivy
      elif command -v grype >/dev/null; then echo grype
      else return 1
      fi
      ;;
    *) echo "unknown scanner preference: $SCANNER_PREF" >&2; return 1 ;;
  esac
}

if ! SCANNER="$(pick_scanner)"; then
  cat >"$OUTDIR/scan-status.json" <<EOF
{
  "schema": "backuplint.release.container_scan.v1",
  "status": "pending",
  "reason": "scanner_unavailable",
  "preferred": ["trivy", "grype"],
  "severity_threshold": "$SEVERITY",
  "install_hint_workshop_only": "Use an ephemeral CI image or a disposable workshop tool install; do not install into production controllers."
}
EOF
  echo "SCANNER_UNAVAILABLE — wrote $OUTDIR/scan-status.json" >&2
  exit 3
fi

RESULT=0
run_trivy_image() {
  local img="$1"
  trivy image --quiet --severity "$SEVERITY" --exit-code 2 \
    --format json --output "$OUTDIR/trivy-image.json" "$img" || RESULT=$?
  trivy image --quiet --severity "$SEVERITY" \
    --format table --output "$OUTDIR/trivy-image.txt" "$img" || true
}

run_trivy_fs() {
  local path="$1"
  trivy fs --quiet --severity "$SEVERITY" --exit-code 2 \
    --format json --output "$OUTDIR/trivy-fs.json" "$path" || RESULT=$?
  trivy fs --quiet --severity "$SEVERITY" \
    --format table --output "$OUTDIR/trivy-fs.txt" "$path" || true
}

run_grype_image() {
  local img="$1"
  # grype exit 0 always unless --fail-on set
  if grype "$img" -o json --file "$OUTDIR/grype-image.json" --fail-on "${SEVERITY%%,*}"; then
    :
  else
    RESULT=2
  fi
  grype "$img" -o table --file "$OUTDIR/grype-image.txt" || true
}

run_grype_fs() {
  local path="$1"
  if grype "dir:$path" -o json --file "$OUTDIR/grype-fs.json" --fail-on "${SEVERITY%%,*}"; then
    :
  else
    RESULT=2
  fi
  grype "dir:$path" -o table --file "$OUTDIR/grype-fs.txt" || true
}

if [[ -n "$IMAGE" ]]; then
  if [[ "$SCANNER" == trivy ]]; then run_trivy_image "$IMAGE"; else run_grype_image "$IMAGE"; fi
fi
if [[ -n "$FS_PATH" ]]; then
  if [[ "$SCANNER" == trivy ]]; then run_trivy_fs "$FS_PATH"; else run_grype_fs "$FS_PATH"; fi
fi

STATUS="pass"
if [[ "$RESULT" -eq 2 ]]; then STATUS="findings_above_threshold"; fi

cat >"$OUTDIR/scan-status.json" <<EOF
{
  "schema": "backuplint.release.container_scan.v1",
  "status": "$STATUS",
  "scanner": "$SCANNER",
  "severity_threshold": "$SEVERITY",
  "image": ${IMAGE:+\"$IMAGE\"}${IMAGE:-null},
  "fs": ${FS_PATH:+\"$FS_PATH\"}${FS_PATH:-null},
  "exit_code": $RESULT
}
EOF

# Fix null JSON when empty — rewrite cleanly
python3 - <<PY
import json
from pathlib import Path
p = Path("$OUTDIR/scan-status.json")
payload = {
  "schema": "backuplint.release.container_scan.v1",
  "status": "$STATUS",
  "scanner": "$SCANNER",
  "severity_threshold": "$SEVERITY",
  "image": """$IMAGE""" or None,
  "fs": """$FS_PATH""" or None,
  "exit_code": int("$RESULT"),
}
p.write_text(json.dumps(payload, indent=2) + "\n")
print(p.read_text())
PY

exit "$RESULT"
