# Release engineering (local rebuilds)

This document describes how to reproduce **local BackupLint 1.0.0** artifacts. The repository already has a private `v1.0.0` tag and GitHub Release; do not create additional public tags or registry publications without owner approval.

## Hard rules

```text
No PyPI publication
No change of repository visibility
No extra public GitHub Release
No extra final release tag
No container registry push
without explicit owner approval
```

Package version is **1.0.0**. Dev strings such as `0.5.0.dev0` are historical only.

## Prerequisites

- Python 3.11+ with `venv`
- `pip install build pip-audit ruff bandit pytest`
- Docker Engine (for controller image / harness gates)
- Clean Git worktree for formal rebuilds

Optional (not required by the local gate):

- Syft / Trivy / Grype for alternate SBOM/vuln scans
- Cosign / Sigstore for future signing (see `release-signing-provenance.md`)

## Quick start

From a clean checkout of the release branch:

```bash
./scripts/release/build-release-candidate.sh --force
```

Outputs land in `./dist-release/`:

```text
dist-release/
  python/                 # wheel + sdist
  sbom/                   # CycloneDX JSON (pip-freeze fallback)
  meta/
    build-manifest.json
    checksums.json
    SHA256SUMS
    artifact-scan.json
    controller-image.json
  evidence/
    release-evidence.json
    clean-room-install.json
    pip-audit.txt
    reproducibility.json  # when second build compare runs
```

## What the gate runs

1. Version consistency (`pyproject.toml` vs `src/backuplint/__init__.py`)
2. `ruff` + Bandit
3. Unit tests (+ controller container integration tests when Docker is available)
4. Clean `git archive` export → wheel/sdist build
5. Artifact hygiene scan (forbidden names/content)
6. Checksums + machine-readable manifest
7. CycloneDX SBOM for wheel install + controller image `pip freeze`
8. Clean-room venv install of wheel **and** sdist (non-editable)
9. `pip-audit` against a clean wheel install
10. Controller image build + hardening harness (unless skipped)
11. Optional second build hash comparison

Scale/endurance/security-audit campaigns are **external evidence** and are not auto-marked passed.

## Clean-room install only

```bash
python scripts/release/clean_room_install.py \
  --wheel dist-release/python/*.whl \
  --sdist dist-release/python/*.tar.gz \
  --expected-version 1.0.0 \
  --report /tmp/clean-room.json
```

## Independent clone gate

```bash
./scripts/release/independent_clone_gate.sh
```

Exports `HEAD` via `git archive` into a temporary directory (no untracked working-tree files), runs the rebuild workflow there, and installs the produced wheel.

## Controller container rebuild checks

The build records:

- image id / labels (including `org.opencontainers.image.revision`)
- non-root user expectation
- healthcheck presence
- absence of embedded `*.key`/`*.pem` under `/app` and `/state`
- SBOM from in-image `pip freeze`

Image is tagged locally (default `backuplint-controller:rc`) and **never pushed** by these scripts.

## Evidence requirements for a real owner-approved release later

| Evidence | Required | Produced by this gate? |
|----------|----------|------------------------|
| Unit/integration results | Yes | Yes (local) |
| Artifact hashes + scan | Yes | Yes |
| SBOM | Yes | Yes (fallback format) |
| Dependency audit | Yes | Yes (`pip-audit`) |
| Controller container harness | Yes for controller shipping | Yes if Docker available |
| Scale campaign | Yes for fleet claims | **No — attach separately** |
| Security/enrollment audit | Yes for fleet claims | **No — attach separately** |
| Signing/provenance | Recommended later | Design only |

## CI

`.github/workflows/release-validation.yml` runs a **non-publishing** validation job on pull requests / manual dispatch. It must never publish packages or create Releases.

## Limitations

- Bit-for-bit reproducibility is **not** claimed unless hashes match in `reproducibility.json`.
- SBOM generator is an in-tree CycloneDX JSON fallback (not Syft).
- Container OS CVE scanning is optional and documented, not mandatory in this gate.
- No artifact signing keys are created or used here.
