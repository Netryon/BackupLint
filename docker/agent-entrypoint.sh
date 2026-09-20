#!/bin/sh
# BackupLint agent container entrypoint (outbound-only).
# Uses exec so SIGTERM reaches the agent process.
set -eu

STATE_DIR="${BACKUPLINT_AGENT_STATE_DIR:-/state}"
CONTROLLER_URL="${BACKUPLINT_AGENT_CONTROLLER_URL:-}"
CA_CERT="${BACKUPLINT_AGENT_CA_CERT:-/config/ca.crt}"
INTERVAL="${BACKUPLINT_AGENT_INTERVAL:-30}"
IDENTITY_DIR="${STATE_DIR}/identity"
COMPOSE_ARGS=""
ONCE_ARGS=""

python /app/docker/agent-preflight.py "${STATE_DIR}"

if [ -z "${CONTROLLER_URL}" ]; then
  echo "ERROR: BACKUPLINT_AGENT_CONTROLLER_URL is required." >&2
  exit 1
fi

# Optional one-shot enrollment when identity is absent.
if [ ! -f "${IDENTITY_DIR}/client.key" ] || [ ! -f "${IDENTITY_DIR}/client.crt" ]; then
  if [ -z "${BACKUPLINT_AGENT_ID:-}" ]; then
    echo "ERROR: identity missing under ${IDENTITY_DIR} and BACKUPLINT_AGENT_ID unset." >&2
    exit 1
  fi
  if [ ! -f "${CA_CERT}" ]; then
    echo "ERROR: CA cert not found at ${CA_CERT} (mount controller ca.crt read-only)." >&2
    exit 1
  fi
  TOKEN_ARGS=""
  if [ -n "${BACKUPLINT_AGENT_TOKEN_FILE:-}" ]; then
    TOKEN_ARGS="--token-file ${BACKUPLINT_AGENT_TOKEN_FILE}"
  elif [ -n "${BACKUPLINT_AGENT_TOKEN_ENV:-}" ]; then
    TOKEN_ARGS="--token-env ${BACKUPLINT_AGENT_TOKEN_ENV}"
  else
    echo "ERROR: provide BACKUPLINT_AGENT_TOKEN_FILE or BACKUPLINT_AGENT_TOKEN_ENV for enrollment." >&2
    exit 1
  fi
  # shellcheck disable=SC2086
  backuplint agent enroll \
    --controller "${CONTROLLER_URL}" \
    --agent-id "${BACKUPLINT_AGENT_ID}" \
    --ca-cert "${CA_CERT}" \
    --identity-dir "${IDENTITY_DIR}" \
    ${TOKEN_ARGS}
fi

if [ -n "${BACKUPLINT_AGENT_COMPOSE:-}" ]; then
  COMPOSE_ARGS="--compose ${BACKUPLINT_AGENT_COMPOSE}"
  if [ -n "${BACKUPLINT_AGENT_CONFIG:-}" ]; then
    COMPOSE_ARGS="${COMPOSE_ARGS} --config ${BACKUPLINT_AGENT_CONFIG}"
  fi
fi

if [ "${BACKUPLINT_AGENT_ONCE:-0}" = "1" ] || [ "${BACKUPLINT_AGENT_ONCE:-}" = "true" ]; then
  ONCE_ARGS="--once"
fi

# shellcheck disable=SC2086
exec backuplint agent run \
  --controller "${CONTROLLER_URL}" \
  --identity-dir "${IDENTITY_DIR}" \
  --interval "${INTERVAL}" \
  ${COMPOSE_ARGS} \
  ${ONCE_ARGS}
