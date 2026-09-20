# Pre-v0.2 Refactor Report

## Before (baseline)

Recorded at commit `c1aff85` before structural changes.

### Status

- `main` matched `origin/main`
- Unit tests: **141 passed**
- Integration tests: **22 passed** (163 total collected)
- Ruff: clean
- Bandit: clean (exit 0)
- pip-audit: no known vulnerabilities
- GitHub Actions on HEAD: **success**
  (https://github.com/Netryon/backuplint/actions/runs/34271232766)

### Startup timings

| Command | Wall time |
|---------|----------:|
| `backuplint --help` | ~0.18 s |
| `backuplint --version` | ~0.09 s |

### Scale timings (`backuplint scan`, bind mounts)

| Services | Wall time |
|---------:|----------:|
| 1 | 160.5 ms |
| 10 | 166.4 ms |
| 50 | 185.4 ms |
| 100 | 205.2 ms |

Bottleneck: Docker Compose `config` subprocess dominates; Python processing is minor.

### Runtime dependencies (`pyproject.toml`)

- `typer>=0.12` (pulls rich, shellingham, annotated-doc)
- `PyYAML>=6.0`

Dev extras: pytest, ruff, bandit, pip-audit.

### Architecture observations

- Clear module split already existed (config → compose/volumes → coverage/restic → reporting → cli).
- Subprocess invocation was duplicated across `compose.py`, `volumes.py`, `restic.py`.
- Path logic already centralized in `paths.py`.
- CLI was already thin; reporting already separate from coverage decisions.

---

## Changes

### Code

- Added `src/backuplint/process.py` with `run_argv()` / `CommandResult` for argv-list execution with timeouts.
- Updated `compose.py`, `volumes.py`, and `restic.py` to use the shared helper (no behavior change).
- Updated unit mocks accordingly.

### Presentation

- Regenerated README GIFs/PNGs from real scans using `/srv/...` paths (no `.venv` prompts, no `/tmp/backuplint-demo` paths).
- Primary fail demo uses postgres / jellyfin / vaultwarden layout matching the product story.
- Renamed Restic GIF to `docs/demo/restic.gif`; added static PNGs.

### Docs

- README media links updated.
- `docs/how-it-works.md`: document Restic password-file precedence over ambient env.
- `docs/testing.md`: remove stale “placeholder” wording; point at production harness.
- Demo README updated for the new renderer flow.

### Dependencies

- No runtime dependency additions or removals.
- Pillow is used only for local demo rendering (not a package dependency).

### Intentionally left alone

- Cache/temporary classification heuristics (product contract).
- WARN vs FAIL for missing named volumes (documented decision).
- Typer/Rich CLI stack.
- No premature plugin architecture or model-layer rewrite.
- No performance micro-optimizations (Compose subprocess dominates).

---

## After

### Status

- See final regression section of the agent report / latest CI on the `pre-v0.2` tag commit.
- Automated test count remains **163** (behavior-preserving refactor).
- Ruff / Bandit / pip-audit: clean after changes.

### Scale timings (post-refactor, same host pattern)

| Services | Wall time |
|---------:|----------:|
| 1 | 159.3 ms |
| 10 | 164.5 ms |
| 50 | 183.1 ms |
| 100 | 203.3 ms |

Within noise of baseline; no meaningful change (as expected).

### Startup (post-refactor)

| Command | Wall time |
|---------|----------:|
| `backuplint --help` | ~0.17 s |
| `backuplint --version` | ~0.09 s |

### Security

- Re-searched for `shell=True`, `os.system`, `eval(`, `exec(` — only documentation mentions of `shell=True`.
- Restic ambient-env override fix retained.
- Bandit clean; pip-audit clean.

### Regression

- Unit 141, integration 22, milestone-6 labs, production harness 53/53, repeated-scan consistency: all green.
