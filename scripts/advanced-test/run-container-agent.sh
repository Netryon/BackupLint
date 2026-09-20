#!/usr/bin/env bash
# Build + smoke the BackupLint container agent against a disposable controller.
# Workshop host only — not the advanced multi-platform campaign.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
EVIDENCE="${BACKUPLINT_AT_EVIDENCE:-${TMPDIR:-/tmp}/backuplint-advanced-test/container-agent}"
IMAGE="${BACKUPLINT_AGENT_TAG:-backuplint-agent:advanced-test}"
CTRL_IMAGE="${BACKUPLINT_CONTROLLER_TAG:-backuplint-controller:hardening-test}"
mkdir -p "$EVIDENCE"
cd "$ROOT"

command -v docker >/dev/null || { echo "PREREQ_MISSING: docker" >&2; exit 3; }
docker info >/dev/null 2>&1 || { echo "PREREQ_MISSING: docker daemon" >&2; exit 3; }

echo "Building agent image $IMAGE ..."
docker build -f Dockerfile.agent -t "$IMAGE" . >"$EVIDENCE/docker-build.log"
echo "$IMAGE" >"$EVIDENCE/image.txt"
git rev-parse HEAD >"$EVIDENCE/sha.txt"
tail -3 "$EVIDENCE/docker-build.log"

if ! docker image inspect "$CTRL_IMAGE" >/dev/null 2>&1; then
  echo "Building controller image $CTRL_IMAGE ..."
  docker build -f Dockerfile.controller -t "$CTRL_IMAGE" .
fi

NET="bl-cagent-net-$$"
VOL_CTRL="bl-cagent-ctrl-$$"
VOL_AGENT="bl-cagent-agent-$$"
CTRL_NAME="bl-cagent-ctrl-$$"
CONFIG="$EVIDENCE/config"
rm -rf "$CONFIG"
mkdir -p "$CONFIG"

cleanup() {
  docker rm -f "$CTRL_NAME" >/dev/null 2>&1 || true
  docker volume rm -f "$VOL_CTRL" "$VOL_AGENT" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create "$NET" >/dev/null
docker volume create "$VOL_CTRL" >/dev/null
docker volume create "$VOL_AGENT" >/dev/null

# Controller hostname must match how agents dial (TLS SAN = container name).
docker run -d --name "$CTRL_NAME" --network "$NET" \
  --user 10001:10001 \
  -e BACKUPLINT_CONTROLLER_HOSTNAME="$CTRL_NAME" \
  -e BACKUPLINT_CONTROLLER_LISTEN=0.0.0.0:8443 \
  -v "${VOL_CTRL}:/state" \
  "$CTRL_IMAGE"

for _ in $(seq 1 90); do
  if docker exec "$CTRL_NAME" python /app/docker/controller-healthcheck.py >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
CTRL_URL="https://${CTRL_NAME}:8443"
echo "controller $CTRL_URL (docker network $NET)"

TOKEN_OUT="$(docker exec "$CTRL_NAME" backuplint controller enroll-token --data-dir /state --label container-agent-smoke)"
AGENT_ID="$(printf '%s\n' "$TOKEN_OUT" | sed -n 's/^agent_id=//p')"
TOKEN="$(printf '%s\n' "$TOKEN_OUT" | sed -n 's/^token=//p')"
test -n "$AGENT_ID" && test -n "$TOKEN"
docker cp "$CTRL_NAME:/state/ca/ca.crt" "$CONFIG/ca.crt"

printf '%s\n' '{"reporting":{"policy_poll_interval":"5m"}}' >"$CONFIG/smoke-policy.json"
docker cp "$CONFIG/smoke-policy.json" "$CTRL_NAME:/tmp/smoke-policy.json"
POLICY_JSON="$(docker exec "$CTRL_NAME" backuplint controller policy create \
  --data-dir /state --display-name "container-agent-smoke" --description "" \
  --settings-file /tmp/smoke-policy.json)"
REVISION_ID="$(printf '%s\n' "$POLICY_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["revision_id"])')"

# Boundary: missing controller URL fails closed
if docker run --rm --network "$NET" --user 10001:10001 -v "${VOL_AGENT}:/state" "$IMAGE" \
  >"$EVIDENCE/missing-url.out" 2>"$EVIDENCE/missing-url.err"; then
  echo "FAIL: missing controller URL should fail" >&2
  exit 2
fi
grep -q "BACKUPLINT_AGENT_CONTROLLER_URL" "$EVIDENCE/missing-url.err"
echo "boundary_missing_url_ok"

# Boundary: read-only state fails closed
if docker run --rm --network "$NET" --user 10001:10001 --read-only \
  -e BACKUPLINT_AGENT_CONTROLLER_URL="$CTRL_URL" \
  -v "${VOL_AGENT}:/state:ro" \
  "$IMAGE" >"$EVIDENCE/ro-state.out" 2>"$EVIDENCE/ro-state.err"; then
  echo "FAIL: read-only state should fail" >&2
  exit 2
fi
echo "boundary_readonly_state_ok"

# Enroll (identity persisted); policy assign requires enrolled agent.
docker run --rm --network "$NET" \
  --read-only --tmpfs /tmp:rw,size=64m \
  --user 10001:10001 \
  -e BACKUPLINT_AGENT_CONTROLLER_URL="$CTRL_URL" \
  -e BACKUPLINT_AGENT_ID="$AGENT_ID" \
  -e BACKUPLINT_AGENT_TOKEN_ENV=BL_TOKEN \
  -e BL_TOKEN="$TOKEN" \
  -e BACKUPLINT_AGENT_CA_CERT=/config/ca.crt \
  -v "${VOL_AGENT}:/state" \
  -v "$CONFIG/ca.crt:/config/ca.crt:ro" \
  --entrypoint backuplint "$IMAGE" \
  agent enroll --controller "$CTRL_URL" --agent-id "$AGENT_ID" \
  --ca-cert /config/ca.crt --identity-dir /state/identity --token-env BL_TOKEN \
  2>&1 | tee "$EVIDENCE/agent-enroll.log"
grep -q "Enrolled agent" "$EVIDENCE/agent-enroll.log"

docker exec "$CTRL_NAME" backuplint controller policy assign "$REVISION_ID" \
  --data-dir /state --agent-id "$AGENT_ID" >/dev/null
echo "policy_assigned revision=$REVISION_ID agent=$AGENT_ID"

# One agent cycle (heartbeat + policy apply)
docker run --rm --network "$NET" \
  --read-only --tmpfs /tmp:rw,size=64m \
  --user 10001:10001 \
  -e BACKUPLINT_AGENT_CONTROLLER_URL="$CTRL_URL" \
  -e BACKUPLINT_AGENT_CA_CERT=/config/ca.crt \
  -e BACKUPLINT_AGENT_ONCE=1 \
  -v "${VOL_AGENT}:/state" \
  -v "$CONFIG/ca.crt:/config/ca.crt:ro" \
  "$IMAGE" 2>&1 | tee "$EVIDENCE/agent-once.log"
grep -q "heartbeat=ok" "$EVIDENCE/agent-once.log"
grep -q "policy=" "$EVIDENCE/agent-once.log"

docker run --rm --user 10001:10001 -v "${VOL_AGENT}:/state" --entrypoint sh "$IMAGE" \
  -c 'test -f /state/identity/client.key && test -f /state/identity/client.crt && echo key_mode=$(stat -c %a /state/identity/client.key)'

py_agent() {
  docker run --rm -i --network "$NET" \
    --user 10001:10001 \
    -e BACKUPLINT_AGENT_CONTROLLER_URL="$CTRL_URL" \
    -v "${VOL_AGENT}:/state" \
    --entrypoint python \
    "$IMAGE" -
}

py_agent <<'PY' | tee "$EVIDENCE/submit-policy.log"
import os
from datetime import UTC, datetime
from pathlib import Path
from backuplint.fleet.agent import AgentIdentity, AgentQueue, FleetAgent
from backuplint.fleet.protocol import ResultEnvelope, new_submission_id

ident = AgentIdentity.load(Path("/state/identity"))
agent = FleetAgent(
    controller_url=os.environ["BACKUPLINT_AGENT_CONTROLLER_URL"],
    identity=ident,
    queue=AgentQueue(Path("/state/identity/queue.jsonl")),
)
agent.heartbeat(max_attempts=3)
sid = new_submission_id()
env = ResultEnvelope(
    agent_id=ident.agent_id,
    submission_id=sid,
    scan_time=datetime.now(UTC).isoformat(),
    backuplint_version="0.8.0.dev0",
    platform="container-agent-smoke",
    result={"summary": {"result": "PASS"}},
)
agent.submit_envelope(env)
agent.submit_envelope(env)  # idempotent retry — must not duplicate-fail
print("SUBMIT_OK", sid)
print("DESIRED", agent.fetch_desired_policy(max_attempts=3).get("status"))
applied = agent.poll_and_apply_policy(max_attempts=2)
print("APPLY", applied.get("apply_status") or applied.get("desired_status"))
PY
grep -q SUBMIT_OK "$EVIDENCE/submit-policy.log"

# Restart without token
docker run --rm --network "$NET" \
  --read-only --tmpfs /tmp:rw,size=64m \
  --user 10001:10001 \
  -e BACKUPLINT_AGENT_CONTROLLER_URL="$CTRL_URL" \
  -e BACKUPLINT_AGENT_ONCE=1 \
  -e BACKUPLINT_AGENT_CA_CERT=/config/ca.crt \
  -v "${VOL_AGENT}:/state" \
  -v "$CONFIG/ca.crt:/config/ca.crt:ro" \
  "$IMAGE" | tee "$EVIDENCE/agent-restart.log"
grep -q "heartbeat=ok" "$EVIDENCE/agent-restart.log"

# Queue while controller down, then drain
docker stop "$CTRL_NAME" >/dev/null
py_agent <<'PY' | tee "$EVIDENCE/queue-outage.log"
from datetime import UTC, datetime
from pathlib import Path
from backuplint.fleet.agent import AgentError, AgentIdentity, AgentQueue, FleetAgent
from backuplint.fleet.protocol import ResultEnvelope, new_submission_id

ident = AgentIdentity.load(Path("/state/identity"))
qpath = Path("/state/identity/queue.jsonl")
agent = FleetAgent(controller_url="https://127.0.0.1:1", identity=ident, queue=AgentQueue(qpath))
sid = new_submission_id()
try:
    agent.submit_envelope(
        ResultEnvelope(
            agent_id=ident.agent_id,
            submission_id=sid,
            scan_time=datetime.now(UTC).isoformat(),
            backuplint_version="0.8.0.dev0",
            platform="container-agent-smoke",
            result={"summary": {"result": "PASS"}},
        )
    )
except AgentError as exc:
    print("EXPECTED_FAIL", type(exc).__name__, sid)
queued = sum(1 for line in qpath.read_text().splitlines() if line.strip()) if qpath.exists() else 0
print("QUEUED", queued)
assert queued >= 1
PY
docker start "$CTRL_NAME" >/dev/null
for _ in $(seq 1 45); do
  docker exec "$CTRL_NAME" python /app/docker/controller-healthcheck.py >/dev/null 2>&1 && break
  sleep 1
done
py_agent <<'PY' | tee "$EVIDENCE/queue-drain.log"
import os
from pathlib import Path
from backuplint.fleet.agent import AgentIdentity, AgentQueue, FleetAgent

ident = AgentIdentity.load(Path("/state/identity"))
qpath = Path("/state/identity/queue.jsonl")
agent = FleetAgent(
    controller_url=os.environ["BACKUPLINT_AGENT_CONTROLLER_URL"],
    identity=ident,
    queue=AgentQueue(qpath),
)
flushed = agent.flush(max_attempts_per_item=5)
left = sum(1 for line in qpath.read_text().splitlines() if line.strip()) if qpath.exists() else 0
print("FLUSHED", flushed, "REMAINING", left)
assert left == 0
PY

docker exec "$CTRL_NAME" backuplint controller revoke --data-dir /state "$AGENT_ID"
py_agent <<'PY' | tee "$EVIDENCE/revoke.log"
import os, sys
from pathlib import Path
from backuplint.fleet.agent import AgentError, AgentIdentity, AgentQueue, FleetAgent

ident = AgentIdentity.load(Path("/state/identity"))
agent = FleetAgent(
    controller_url=os.environ["BACKUPLINT_AGENT_CONTROLLER_URL"],
    identity=ident,
    queue=AgentQueue(Path("/state/identity/queue.jsonl")),
)
try:
    agent.heartbeat(max_attempts=2)
except AgentError as exc:
    print("REVOKED_REJECTED", exc.message[:160])
    sys.exit(0)
print("UNEXPECTED_SUCCESS")
sys.exit(2)
PY
grep -q REVOKED_REJECTED "$EVIDENCE/revoke.log"

if docker save "$IMAGE" | tar -t | grep -Ei '(^|/)[^/]+\.key$|/token|/password' ; then
  echo "FAIL: suspicious secret-like paths in image layers" >&2
  exit 2
fi
echo "layer_inspection_ok"
echo "mixed_topology=container-agent->container-controller"
echo "CONTAINER_AGENT_SMOKE_OK evidence=$EVIDENCE agent_id=$AGENT_ID"
