# Validation evidence (public summary)

This document summarizes **what was tested** for BackupLint **v1.0.0**. It is not a substitute for the in-tree test suite (`tests/unit`, `tests/integration`, CI in `.github/workflows/ci.yml`).

Detailed raw campaign logs, SQLite databases, private addresses, and lab topology remain in the **private** engineering archive. They are not published in this repository.

## Product tests you can re-run

- Unit tests: classification, coverage, fleet protocol, policy, queue accounting
- Integration tests: Docker Compose fixtures and Restic (when Docker/Restic are available on the runner)
- `ruff`, Bandit, `pip-audit` in CI

## Campaign-scale proofs (current release lineage)

Facts below are scoped to the v1.0.0 candidate. Historical older-milestone reports are not a current PASS by themselves.

| Area | Public-safe result |
| --- | --- |
| Coverage | Compose mounts vs `backup_paths` and vs Restic snapshot roots |
| Restic integrity | Standard `restic check`; opt-in deep `--read-data` where the platform allowed |
| Restore verification | Isolated restore into BackupLint-owned temp paths; independent restored-file hash match in campaign scope |
| Endurance | Mixed native/container fleet ran **72+ hours** with exact queue/event accounting on the current stack |
| Controller sizing | Measured management-plane checkpoint **through 500 agents** (about 6–8 MB/agent/day on the wire at the tested 30s cadence). **Excludes Restic backup payload.** Not certified above 500 agents. A larger historical saturation campaign (2,000 agents) informed design; it is **not** a v1 certified production capacity. |
| Real multi-VM campaign | Debian / Rocky / Ubuntu-class hosts with native and container topologies |
| Fedora 43 | Current-candidate platform proof (x86_64) |
| Raspberry Pi 4 | Current-candidate ARM64 proof (Raspberry Pi OS). Raspberry Pi **3** is not claimed. |
| Mixed topology | Native controller + container agent, container controller + native agent, container+container |
| SIEM | Outage isolation and drain/recovery of the durable export queue |
| Central policy | Assignment, apply, drift, staged rollout, rollback in campaign scope |
| Revocation | Enrolled agent can be revoked; mTLS thereafter fails as designed |
| Controller recovery | Restart reconnect; state lives on the data directory / volume (see [controller-recovery.md](controller-recovery.md)) |
| Containers | Digest-pinned base; Trivy scan after a base refresh that removed an avoidable pcre2 High; remaining Critical/High classified as residual / not a zero-CVE claim |
| Agent container | uid **10001**, unprivileged, no baked secrets, no Docker socket requirement |

WSL2 is unsupported.

## How to read FAIL vs ERROR vs presence

See [architecture.md](architecture.md). Tests treat a missing repository as operational **ERROR**, not coverage **FAIL**. Dashboard online/stale/offline is not audit PASS/FAIL.

## Artifacts

Wheel, sdist, and `SHA256SUMS` for this release are on the public [v1.0.0 GitHub Release](https://github.com/Netryon/BackupLint/releases/tag/v1.0.0). Install from [PyPI](https://pypi.org/project/backuplint/1.0.0/) (`backuplint==1.0.0`) or pull `ghcr.io/netryon/backuplint-controller:1.0.0` and `ghcr.io/netryon/backuplint-agent:1.0.0`. Treat any other locally built artifacts as unofficial.

CI on `main` does not write repository secrets to logs.
