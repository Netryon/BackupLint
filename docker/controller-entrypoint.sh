#!/bin/sh
# BackupLint controller container entrypoint.
# Uses exec so SIGTERM/SIGINT reach the Python controller process.
# No secrets are baked into the image; durable state lives under DATA_DIR.
set -eu

DATA_DIR="${BACKUPLINT_CONTROLLER_DATA_DIR:-/state}"
LISTEN="${BACKUPLINT_CONTROLLER_LISTEN:-0.0.0.0:8443}"
HOSTNAME_ARG="${BACKUPLINT_CONTROLLER_HOSTNAME:-localhost}"
DASHBOARD_ARGS=""

# Fail closed with a clear message if /state is missing or not writable.
python /app/docker/controller-preflight.py "${DATA_DIR}"

# Optional dashboard: set BACKUPLINT_CONTROLLER_DASHBOARD=1 and ensure
# ${DATA_DIR}/dashboard/password.scrypt exists (or BACKUPLINT_DASHBOARD_PASSWORD_FILE).
if [ "${BACKUPLINT_CONTROLLER_DASHBOARD:-0}" = "1" ] \
  || [ "${BACKUPLINT_CONTROLLER_DASHBOARD:-}" = "true" ]; then
  DASHBOARD_ARGS="--dashboard"
  if [ -n "${BACKUPLINT_DASHBOARD_PASSWORD_FILE:-}" ]; then
    DASHBOARD_ARGS="${DASHBOARD_ARGS} --dashboard-password-file ${BACKUPLINT_DASHBOARD_PASSWORD_FILE}"
  fi
fi

SIEM_ARGS=""
if [ -n "${BACKUPLINT_SIEM_CONFIG_FILE:-}" ]; then
  SIEM_ARGS="--siem-config ${BACKUPLINT_SIEM_CONFIG_FILE}"
elif [ "${BACKUPLINT_CONTROLLER_SIEM:-0}" = "1" ] \
  || [ "${BACKUPLINT_CONTROLLER_SIEM:-}" = "true" ]; then
  if [ -z "${BACKUPLINT_SIEM_CONFIG_FILE:-}" ]; then
    echo "ERROR: BACKUPLINT_CONTROLLER_SIEM is set but BACKUPLINT_SIEM_CONFIG_FILE is empty." >&2
    exit 1
  fi
fi

# shellcheck disable=SC2086
exec backuplint controller run \
  --data-dir "${DATA_DIR}" \
  --listen "${LISTEN}" \
  --hostname "${HOSTNAME_ARG}" \
  ${DASHBOARD_ARGS} \
  ${SIEM_ARGS}
