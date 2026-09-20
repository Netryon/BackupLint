#!/usr/bin/env bash
set -euo pipefail
DISTRO="${1:-}"
[[ "$DISTRO" =~ ^(debian|fedora|rocky)$ ]] || {
  echo "usage: $0 <debian|fedora|rocky>" >&2
  exit 3
}
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
EVIDENCE="${BACKUPLINT_AT_EVIDENCE:-${TMPDIR:-/tmp}/backuplint-advanced-test/distro-$DISTRO}"
mkdir -p "$EVIDENCE"
GUEST="${BACKUPLINT_GUEST:-}"
[[ -n "$GUEST" ]] || {
  echo "PREREQ_MISSING: set BACKUPLINT_GUEST to ssh target for $DISTRO" >&2
  echo "Example: BACKUPLINT_GUEST=user@debian-vm $0 $DISTRO" >&2
  exit 3
}
echo "Will run guest validate on $GUEST — capturing to $EVIDENCE"
ssh "$GUEST" "uname -a; cat /etc/os-release | head -5" | tee "$EVIDENCE/guest-identity.txt"
echo "Next on guest: clone/checkout SHA, venv, ruff, pytest unit, guest-validate.sh"
echo "Use $ROOT/scripts/pre-v1-platform-matrix.sh for full command blocks."
echo "Do not mark PASS without guest evidence files under $EVIDENCE"
