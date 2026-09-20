#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
EVIDENCE="${BACKUPLINT_AT_EVIDENCE:-${TMPDIR:-/tmp}/backuplint-advanced-test/arm-pi}"
mkdir -p "$EVIDENCE"
ARCH="$(uname -m)"
echo "host_arch=$ARCH" | tee "$EVIDENCE/arch.txt"
if [[ "$ARCH" != aarch64 && "$ARCH" != armv7l && "$ARCH" != arm64 ]]; then
  echo "PREREQ_MISSING: this host is $ARCH; run on real ARM/Pi hardware" >&2
  exit 3
fi
echo "ARM host detected. Run unit suite + dashboard-platform-smoke.py and capture logs to $EVIDENCE"
echo "sha=$(git -C "$ROOT" rev-parse HEAD)"
