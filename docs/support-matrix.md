# BackupLint v1.0.0 support matrix

Current-candidate evidence for the v1.0.0 release metadata lineage (product code from the validated acceptance candidate; packaging includes the digest-pinned container base). Historical milestone reports are **not** a current PASS by themselves.

| Platform / mode | Current-candidate v1 | Notes |
| --- | --- | --- |
| Ubuntu x86_64 | **tested** | Host and multi-VM acceptance |
| Debian x86_64 | **tested** | Multi-VM acceptance |
| Rocky Linux 9 x86_64 | **tested** | Multi-VM acceptance (Python 3.11+) |
| Fedora 43 x86_64 | **tested** | Current-candidate platform proof |
| Raspberry Pi 4 / Raspberry Pi OS ARM64 | **tested** | Current-candidate platform proof |
| Raspberry Pi 3 ARM64 | **not claimed** | Historical older-milestone tests exist; no current-candidate proof for this release |
| WSL2 | **unsupported** | Not claimed |
| Native standalone (`backuplint scan`) | **tested** | |
| Native controller + agent | **tested** | CSR enroll, mTLS, heartbeat, submit, policy |
| Controller container | **tested** | Mixed topology |
| Agent container | **tested** | Non-root uid 10001; no Docker socket; mixed topology |
| Dashboard (browser) | **tested** | Playwright / real dashboard smokes |
| SIEM HTTPS export | **tested** | External receiver / outage-drain in campaign scope |
| Central policy GitOps | **tested** | Assign, apply, drift/rollout in campaign scope |
| Restic coverage + standard integrity + isolated restore | **tested** | Deep restore not required on resource-constrained Pi |
| Controller sizing / management network | **tested through 500 agents** | Not certified beyond 500 |
| Container image vulnerability scan | **tested** | Residual base CVEs remain; not a zero-CVE claim |
| >=72-hour mixed endurance | **tested** | Exact-accounting campaign on the current stack |

## How to read this

- **tested** = closed on the v1.0.0 current-candidate evidence lineage for the documented scope
- **not claimed** / **unsupported** = do not document as a v1.0.0 current-candidate proof
