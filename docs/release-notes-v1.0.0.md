# BackupLint v1.0.0 release notes

Public draft for the `1.0.0` package. Not a GitHub Release and not a publication approval.

## What BackupLint is

BackupLint is an independent **backup-assurance** layer for Docker Compose stacks. It is **not** a backup engine, remote shell, or disaster-recovery orchestrator. It does not replace your backup jobs.

## Main v1 capabilities

- Compose mount discovery and coverage against `backup_paths` and/or Restic snapshot roots
- Optional Restic **standard** and opt-in **deep** integrity
- Optional isolated restore verification into BackupLint-owned temporary paths
- Local scheduler (`backuplint daemon` / `schedule`)
- Fleet controller + agent over HTTPS with mTLS after unique CSR enrollment
- Optional read-only dashboard (session auth)
- Optional SIEM HTTPS JSON export with a durable queue
- Central policy / GitOps (immutable revisions, assignment, drift, staged rollout)
- Native installer profiles and optional controller/agent containers

## Supported backup engine

**Restic only** in v1. Borg and Kopia are not supported.

Coverage, integrity, restore verification, and disaster recovery are different questions. Standard `restic check` does not read every stored byte. `restic check --read-data` is not a successful restore. A filesystem restore does not prove application consistency. A missing repository is an operational **ERROR**, not a coverage **FAIL**. Dashboard **presence** (online/stale/offline) is not audit health.

## Supported / tested platforms

See [support-matrix.md](support-matrix.md).

Current-candidate tested: Ubuntu, Debian, Rocky 9, Fedora 43 (x86_64), Raspberry Pi 4 ARM64 / Raspberry Pi OS, native and container controller/agent including mixed topologies.

Not claimed as current-candidate: Raspberry Pi 3. WSL2 unsupported.

## Deployment forms

- Standalone CLI on the Compose host
- Native fleet controller and agent
- Container controller and outbound-only agent (non-root, no Docker socket by default)
- Mixed native/container topologies

## Dashboard

Optional read-only fleet UI. Presence and audit state remain separate. No dashboard writes to policy.

## SIEM

Optional HTTPS JSON export with durable queuing. Healthy PASS events are not the same as exported failure families. Delivery errors are isolated from backup-assurance event families.

## Central policy / GitOps

Immutable policy revisions, per-agent or default assignment, apply/drift reporting, and staged rollout. Newly enrolled agents may need an explicit assignment before `policy=success`.

## Major security / reliability validation

- Multi-VM real-fleet acceptance, Fedora 43 and Pi 4 current-candidate proofs
- Controller sizing/network certification through **500 agents** (management plane only; Restic payload excluded)
- Endurance on the current stack for the documented duration
- Trivy scan of digest-pinned container images; pcre2 High removed by base refresh; residual OS/app findings remain (not zero CVEs)
- Agent image: uid 10001, unprivileged, no baked secrets, no Docker socket requirement

## Known limitations / non-goals

- Not a backup engine; does not backup, prune, or restore over live data
- Does not modify Compose files or volumes
- Does not claim WSL2
- Does not claim certified capacity beyond the tested 500-agent workload
- Does not claim zero container CVEs
- Unit tests alone are not production readiness
- Isolated restore verification is not a full DR drill
