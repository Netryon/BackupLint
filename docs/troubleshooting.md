# Troubleshooting

## `backuplint --version` is not 1.0.0

You are not running the v1.0.0 install. Check `which backuplint` and the active venv. `pip show backuplint`.

## Scan cannot talk to Docker

`backuplint scan` needs Docker Engine, the Compose plugin (`docker compose`), and permission to the Docker API (typically membership in the `docker` group). Re-login after group changes.

## Named volume is WARN, not FAIL

If the volume does not exist locally, BackupLint cannot prove a host path. That is a **WARN** (review required), not a coverage FAIL.

## Restic ERROR instead of FAIL

An unreachable, missing, or unreadable repository is an operational **ERROR**. Unprotected persistent mounts are coverage **FAIL**. They are different outcomes.

## Agent enroll fails TLS / hostname mismatch

The controller server certificate SAN must include the hostname or IP in `--controller` / `BACKUPLINT_AGENT_CONTROLLER_URL`. Changing `BACKUPLINT_CONTROLLER_HOSTNAME` after first start does not reissue `server.crt`; delete `server/` certs deliberately if the SAN must change (this rotates controller identity for that cert).

Copy `ca.crt` from the controller data dir / volume. Agents must trust that CA.

## Enrollment token already used

Tokens are one-time. Mint a new `enroll-token`. Do not reuse tokens across hosts.

## Heartbeat works but policy is not success

Newly enrolled agents may have `NO_ASSIGNMENT` until you assign a policy revision. Presence (online) is not policy apply and is not audit PASS.

## Permission denied on `/state` (containers)

The volume must be writable by uid **10001**. Recreate or `chown 10001:10001` bind mounts. Do not run the image as root to “fix” this.

## Dashboard login fails

Confirm dashboard is enabled and the password file exists under the controller data directory. Session cookies require HTTPS to the controller the browser actually opened.

## Controller will not start after copy

SQLite, WAL, and SHM must stay together. Prefer a clean stop (SIGTERM) before filesystem snapshot. See [controller-recovery.md](controller-recovery.md).

## systemd unit missing

Prefix/lab installs do not install host units. System installs use [native-installer.md](native-installer.md) and `docs/systemd/`.

## Container agent cannot scan Compose

Compose submit needs Docker tooling and usually a Docker socket. That is **not** the default fleet agent image posture. Run `backuplint scan` / submit on the Compose host (native agent) instead, or accept the socket blast radius explicitly.

## Further reading

[installation.md](installation.md), [docker.md](docker.md), [fleet.md](fleet.md), [security.md](security.md).
