#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
EVIDENCE="${BACKUPLINT_AT_EVIDENCE:-${TMPDIR:-/tmp}/backuplint-advanced-test/container-controller}"
mkdir -p "$EVIDENCE"
command -v docker >/dev/null || { echo "PREREQ_MISSING: docker" >&2; exit 3; }
[[ -f "$ROOT/Dockerfile.controller" ]] || { echo "PREREQ_MISSING: Dockerfile.controller" >&2; exit 3; }
TAG="${BACKUPLINT_CONTROLLER_TAG:-backuplint-controller:advanced-test}"
docker build -f "$ROOT/Dockerfile.controller" -t "$TAG" "$ROOT"
echo "$TAG" >"$EVIDENCE/image.txt"
git -C "$ROOT" rev-parse HEAD >"$EVIDENCE/sha.txt"
echo "CONTAINER_CONTROLLER_IMAGE_OK tag=$TAG"
echo "Start with documented docker run from Dockerfile/README; do not publish."
