#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
EVIDENCE="${BACKUPLINT_AT_EVIDENCE:-${TMPDIR:-/tmp}/backuplint-advanced-test/native-agent}"
CTRL_URL="${1:-}"
CA_CERT="${2:-}"
TOKEN="${3:-}"
mkdir -p "$EVIDENCE"
[[ -n "$CTRL_URL" && -n "$CA_CERT" && -n "$TOKEN" ]] || {
  echo "usage: $0 <controller_url> <ca.crt> <enroll_token>" >&2
  echo "PREREQ_MISSING: controller enrollment materials" >&2
  exit 3
}
[[ -f "$CA_CERT" ]] || { echo "PREREQ_MISSING: ca cert $CA_CERT" >&2; exit 3; }
export PYTHONPATH="$ROOT/src"
python3 - <<PY
from pathlib import Path
from backuplint.fleet.agent import AgentQueue, FleetAgent
ident = FleetAgent.enroll_with_ca(
    controller_url="$CTRL_URL",
    token="$TOKEN",
    ca_cert=Path("$CA_CERT"),
    identity_dir=Path("$EVIDENCE") / "identity",
    hostname="advanced-native-agent",
)
agent = FleetAgent(
    controller_url="$CTRL_URL",
    identity=ident,
    queue=AgentQueue(Path("$EVIDENCE") / "q.jsonl"),
    sleep=lambda _s: None,
)
agent.heartbeat(max_attempts=3)
print("NATIVE_AGENT_ENROLL_HB_OK", ident.agent_id)
PY
git -C "$ROOT" rev-parse HEAD >"$EVIDENCE/sha.txt"
