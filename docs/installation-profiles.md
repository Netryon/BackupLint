# Installation roles, profiles, and install plans

BackupLint is **one product/package**. Installation is role-aware:

```text
standalone | agent | controller | all_in_one
```

Runtime configuration chooses which **installed** components are **enabled**.
Profile/config processing **never silently downloads or installs** missing
dependencies. Planning and validation report what is missing.

> Interactive installer UI and unattended native execution are documented in
> [native-installer.md](native-installer.md). This document describes the
> profile/plan foundation used by both paths.

## Concepts

| Concept | Meaning |
|---------|---------|
| **Role** | Why this node exists (local checks, fleet agent, controller, or combined). |
| **Feature** | Installable/enableable capability (`coverage`, `integrity`, `restore_verification`, `scheduler`, `fleet_agent`, `fleet_controller`). |
| **Deployment form** | `native` or `container`. Mixed fleets are allowed; form must not change fleet protocol semantics. |
| **Profile** | Versioned YAML/JSON describing role, deployment, and feature flags. |
| **Install plan** | Structured dry-run output of requirements (modules, binaries, state dirs, warnings, missing items). |
| **Installed vs enabled** | Support may be present on disk while disabled in config, or requested while support is absent. |

## Roles

- **standalone** — local verification + optional scheduler; no fleet controller.
- **agent** — local verification + outbound fleet reporting; no local controller.
- **controller** — fleet controller; local scan/Restic/Docker not required unless explicitly selected.
- **all_in_one** — local verification + controller on the same host.

Controller does **not** imply “multiple servers”; topology is an operations choice.

## Profile schema (v1)

```yaml
schema_version: 1
role: agent
deployment: native
features:
  coverage: true
  integrity: true
  restore_verification: false
  scheduler: true
  # fleet_agent defaults true for role agent
```

Rules (non-exhaustive):

- Unknown roles, deployments, features, or top-level fields are rejected.
- Unsupported schema versions are rejected (no silent reinterpretation).
- `agent` requires `fleet_agent: true`.
- `controller` / `all_in_one` require `fleet_controller: true`.
- `standalone` cannot enable fleet agent/controller features.
- `scheduler` requires at least one of coverage / integrity / restore_verification.
- No secret fields and no arbitrary command/script fields.

### Examples

**Controller (native):**

```yaml
schema_version: 1
role: controller
deployment: native
```

**Standalone with restore verification:**

```yaml
schema_version: 1
role: standalone
deployment: native
features:
  coverage: true
  integrity: true
  restore_verification: true
  scheduler: true
```

**All-in-one lab host:**

```yaml
schema_version: 1
role: all_in_one
deployment: native
```

## Deployment support

The model distinguishes:

```text
architecture valid  ≠  currently implemented
```

| Role × deployment | Status (foundation) |
|-------------------|---------------------|
| any × native | implemented |
| controller × container | architecture valid, planned (controller-container track) |
| agent × container | architecture valid, limited (agent host-access model not ready) |
| standalone × container | architecture valid, unsupported |
| all_in_one × container | invalid / unsupported |

## Install plan

Given a validated profile, `resolve_install_plan()` returns structured data:

- selected features
- Python modules / project components
- external binaries (`docker`, `restic`, `openssl`, …)
- state directory expectations
- privilege / host-access warnings
- missing requirements (when host probing is enabled)
- recommended internal packaging extras (`core`, `scan`, `controller`, `agent`)

Public packaging extras are **not** locked yet; the plan records the recommended
split for a future packaging decision.

### CLI dry-run (foundation)

```bash
backuplint profile validate path/to/profile.yaml
backuplint profile plan path/to/profile.yaml
backuplint profile plan path/to/profile.yaml --no-probe
```

These commands never install packages. JSON output is intended for installers,
Ansible, cloud-init, bundle generators, and tests.

## Installed vs enabled

```text
installed ≠ enabled
```

If restore verification is requested but Restic/support is absent, diagnostics
look like:

```text
restore verification requested but support is not installed (missing: restic)
```

BackupLint must not fetch the dependency automatically.

## Dependency detection

Safe, non-destructive probes (no root, no package installs):

- `restic version`
- `docker version` (client)
- `openssl version`
- Python module importability for feature support

## Import boundaries

CLI commands lazy-import heavy local-scan modules (`audit`, `compose`, `restic`,
scheduler) so controller-oriented entry points do not eagerly load Docker/Restic
code. The `backuplint.install` package does not import scan/Restic code.

## Future installer work

Not in this foundation:

- interactive installer wizard
- remote SSH provisioning
- vendor adapters
- automatic package installation
- final public packaging extra names (documented as recommendations only)
