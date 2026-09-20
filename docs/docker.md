# Docker deployment

Controller and agent images are **additive**. They do not replace native installs. Base image is digest-pinned `python:3.12-slim-bookworm`. Runtime user is **uid/gid 10001**. Privileged mode is not required. The Docker socket is **not** required for fleet heartbeat, enrollment, or policy.

Do not treat remaining OS/library CVEs in the base image as a “zero CVE” claim. See [security.md](security.md).

## Build locally

```bash
docker build -f Dockerfile.controller -t backuplint-controller:1.0.0 .
docker build -f Dockerfile.agent -t backuplint-agent:1.0.0 .
```

Confirm:

```bash
docker run --rm --entrypoint backuplint backuplint-controller:1.0.0 --version
docker run --rm --user 10001:10001 --entrypoint id backuplint-agent:1.0.0
```

Images are not published to Docker Hub or GHCR as part of this tree’s default workflow.

## Persistent volumes

| Service | Mount | Contents |
| --- | --- | --- |
| Controller | `/state` (rw, owned by 10001) | CA, server TLS, SQLite, optional dashboard hash, optional SIEM queue |
| Agent | `/state` (rw, owned by 10001) | `identity/` private key, cert, CA copy, durable queue |

Bind-mounts must be `chown 10001:10001` or the entrypoint preflight fails. Prefer named volumes.

Read-only root filesystem is supported when `/state` is a volume and `/tmp` is tmpfs. See [controller-container.md](controller-container.md) and [agent-container.md](agent-container.md).

## Controller container

```bash
docker run --rm --name backuplint-controller \
  -p 8443:8443 \
  -e BACKUPLINT_CONTROLLER_HOSTNAME=controller.example \
  -v backuplint-controller-state:/state \
  backuplint-controller:1.0.0
```

Hardened sketch: `--read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp`. Compose files: `deploy/controller/compose.yml` and `deploy/controller/compose.hardened.yml`.

## Agent container

First-boot enrollment uses a one-time token plus the controller CA. The agent private key is generated **inside** `/state` (CSR enrollment).

```bash
docker run --rm --name backuplint-agent \
  --user 10001:10001 \
  --read-only \
  --tmpfs /tmp:rw,size=64m \
  -e BACKUPLINT_AGENT_CONTROLLER_URL=https://controller.example:8443 \
  -e BACKUPLINT_AGENT_ID=agent-... \
  -e BACKUPLINT_AGENT_TOKEN_ENV=BL_TOKEN \
  -e BL_TOKEN=... \
  -v backuplint-agent-state:/state \
  -v "$PWD/ca.crt:/config/ca.crt:ro" \
  backuplint-agent:1.0.0
```

Do not pass `--privileged`. Do not mount `/var/run/docker.sock` unless you intentionally enable Compose submit from that container.

## Topology examples

### Container controller + container agent

1. Start the controller with a published `8443` and a hostname/IP in `BACKUPLINT_CONTROLLER_HOSTNAME` that matches the URL agents will use (certificate SAN).
2. Copy `ca.crt` out of the controller volume.
3. Mint `enroll-token` via `docker exec … backuplint controller enroll-token`.
4. Start the agent with token + CA as above.

Use a user-defined Docker network so the agent can resolve the controller by service name.

### Container controller + native agent

Run the controller as above. On the Compose host:

```bash
backuplint agent enroll \
  --controller https://controller.example:8443 \
  --token "$ENROLL_TOKEN" --agent-id "$AGENT_ID" \
  --ca-cert ./ca.crt --identity-dir ./bl-agent
```

### Native controller + container agent

Run `backuplint controller run` on the host. Point the agent container at `https://host.docker.internal:8443` (or the host IP) **if that name/IP is in the server certificate SAN**.

### Mixed fleets

Native and container agents may enroll in the same controller. Presence and audit health stay separate regardless of deployment form.

## Upgrades and persistence

Replace the image tag, keep the same `/state` volumes. Destroying the controller volume creates a new fleet CA; agents must re-enroll. Destroying the agent volume loses the private key; re-enroll with a new token.
