# Fleet agent container

Run the BackupLint **fleet agent** in a container with outbound-only HTTPS/mTLS
to a controller. This uses the same product package and protocol as the native
agent (`backuplint agent enroll` / `backuplint agent run`).

## Scope

| Claim | Status |
|-------|--------|
| Packaging + basic x86_64 workshop smoke | this document / current branch |
| Cross-platform / advanced mixed production proof | **deferred** to advanced campaign |

## What is included

- Same BackupLint package as native installs
- CSR enrollment (agent-generated private key; key stays in `/state`)
- Long-running loop: heartbeat, durable queue flush, policy desired/apply
- Optional Compose scan/submit when `--compose` / `BACKUPLINT_AGENT_COMPOSE` is set
  (**requires** Docker tooling + socket — not default)

## What is not included / not default

- Docker socket
- Privileged mode
- Host networking requirement
- Restic / Compose tooling baked for privilege
- Baked-in tokens, private keys, or CA material

## Build

```bash
docker build -f Dockerfile.agent -t backuplint-agent:local .
```

## Persistent state

Mount a writable volume at `/state` (UID/GID **10001** by default):

```text
/state/identity/
  client.key    # PRIVATE — agent-held — mode 0600
  client.crt
  ca.crt        # controller CA copy as stored by enroll
  queue.jsonl   # durable outbound queue
  managed-policy/   # applied central policy state
```

## Required mounts / env

| Mount / env | Purpose |
|-------------|---------|
| `/state` (rw) | identity + queue |
| `/config/ca.crt` (ro) | controller CA for first enrollment |
| `BACKUPLINT_AGENT_CONTROLLER_URL` | e.g. `https://controller:8443` |
| `BACKUPLINT_AGENT_ID` | from enroll-token (first boot) |
| `BACKUPLINT_AGENT_TOKEN_FILE` or `_TOKEN_ENV` | one-time token (first boot) |

Optional: `BACKUPLINT_AGENT_ONCE=1` for a single cycle (smoke).

## Example (container agent → native or container controller)

```bash
# After controller is up and enroll-token minted:
docker run --rm \
  --read-only \
  --tmpfs /tmp:rw,size=64m \
  --user 10001:10001 \
  -e BACKUPLINT_AGENT_CONTROLLER_URL=https://host.docker.internal:8443 \
  -e BACKUPLINT_AGENT_ID=agent-... \
  -e BACKUPLINT_AGENT_TOKEN_ENV=BL_TOKEN \
  -e BL_TOKEN=... \
  -e BACKUPLINT_AGENT_ONCE=1 \
  -v "$PWD/agent-state:/state" \
  -v "$PWD/ca.crt:/config/ca.crt:ro" \
  backuplint-agent:local
```

Do **not** pass `--privileged` or mount `/var/run/docker.sock` unless you are
intentionally enabling Compose-based submit and understand the blast radius.

## Fail-closed behavior

- Missing/unwritable `/state` → exit non-zero (preflight)
- Missing CA/token/agent_id on first boot → exit non-zero
- Revoked agent certificates → controller rejects subsequent calls

## Related

- [controller-container.md](controller-container.md)
- [fleet.md](fleet.md)
- [central-policy.md](central-policy.md)
