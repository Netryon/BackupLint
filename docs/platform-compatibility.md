# Platform compatibility

**v1.0.0 current-candidate platforms** are listed in [support-matrix.md](support-matrix.md) (Ubuntu, Debian, Rocky 9, Fedora 43 x86_64, Raspberry Pi **4** ARM64). Raspberry Pi **3** is **not** a current-candidate proof for v1.0.0.

The tables below are **historical v0.2/v0.3 campaign notes** (coverage + integrity + optional restore). They must not be read as a Pi 3 current-candidate claim.

Cross-platform validation for BackupLint **v0.3** (coverage + integrity + optional restore verification). Originally recorded for (coverage + optional Restic integrity).
WSL2 was not run in this campaign.

## Method

Guests received:

1. Editable install (`pip install -e ".[dev]"`)
2. `ruff`, Bandit, `pip-audit`
3. Unit tests (187) and integration tests (30)
4. Milestone 6 coverage labs
5. Classic production validation harness
6. v0.2 integrity campaign (healthy / deep / corruption / auth / state / JSON / reliability / scale)
7. Output reliability check (five identical scans)

x86_64 guests were LXD VMs on the Ubuntu host. ARM64 used physical Raspberry Pi boards
reached over SSH from the same host.

Automation lives in:

- `scripts/platform-inventory.sh`
- `scripts/guest-validate.sh`
- `scripts/provision-and-validate-vm.sh`
- `test-lab/production/run-v02-integrity-campaign.sh`

Per-guest artifacts: `docs/platform-reports/v02-*/` (workshop only).

## Results (v0.2)

| Platform | Version | Arch | Python used | Docker / Compose | Restic | SELinux | Unit | Integration | Production | Integrity campaign |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Ubuntu (host baseline) | 24.04-class | x86_64 | 3.12+ | Engine + plugin | ~0.18.1 | N/A | 187 pass | 30 pass | ALL PASSED | 45/45 PASS |
| Debian | 12 (bookworm) | x86_64 | 3.11.2 | 29.8.0 / v5.5.1 | **0.14.0** | N/A | 187 pass | 30 pass | ALL PASSED | PASS |
| Fedora | 43 | x86_64 | 3.14.7 | 29.8.0 / v5.5.1 | **0.19.1** | Permissive (cloud default) | 187 pass | 30 pass | ALL PASSED | 45/45 PASS |
| Rocky Linux | 9.8 | x86_64 | **3.11.13** (AppStream; default is 3.9) | 29.8.0 / v5.5.1 | **0.19.1** (EPEL) | Permissive for LXD harness | 187 pass | 30 pass | ALL PASSED | 45/45 PASS |
| Raspberry Pi 4 Model B (Pi OS) | 13 (trixie) | **aarch64** | 3.13.5 | 29.8.0 / v5.5.1 | **0.18.0** | N/A | 187 pass | 30 pass | ALL PASSED | 45/45 PASS |
| Raspberry Pi 3 Model B (Pi OS) | 13 (trixie) | **aarch64** | 3.13.5 | 29.8.0 / v5.5.1 | **0.18.0** | N/A | 187 pass | 30 pass | ALL PASSED | 45/45 PASS |

Raspberry Pi **3 Model B** ARM64 was directly tested (~905 MiB RAM + existing zram).
Raspberry Pi **3B+** is expected/supported on the same Raspberry Pi OS ARM64 baseline
but was **not** separately benchmarked in this campaign.

AlmaLinux 9 was not launched separately. It shares the Rocky/RHEL 9 package layout;
expect the same requirements (`python3.11`, EPEL restic, Docker CE).

## Restic compatibility

Integrity classification was validated across the installed matrix range approximately
**0.14 through 0.19**. Classification uses sanitized Restic output as well as exit
status (older Restic may use exit `1` for several failure classes). BackupLint does
not depend on `restic check --json`.

Standard integrity (`restic check`) is not equivalent to reading every stored backup
byte. Deep integrity (`restic check --read-data`) is slower and still is not restore
verification.

## Install notes by family

### Debian 12 (x86_64) and Debian 13 / Raspberry Pi OS (arm64)

- Install Docker from `download.docker.com` (Engine + Compose plugin).
- Distro `python3` / `python3-venv` meets `requires-python >=3.11` on these images.
- Upgrade `pip` / `setuptools` / `wheel` inside the venv so `pip-audit` matches CI
  (cloud images can leave an old setuptools).

### Fedora 43

- Docker CE repo file: `/etc/yum.repos.d/docker-ce.repo` (dnf5 no longer accepts
  `config-manager --add-repo`).
- Cloud image SELinux mode was **Permissive**.

### Rocky Linux 9 (and AlmaLinux 9 by analogy)

- Default `python3` is **3.9** and cannot install BackupLint (`requires-python >=3.11`).
- Install AppStream `python3.11` / `python3.11-pip` and create the venv with that
  interpreter (`guest-validate.sh` prefers `python3.11` when present).
- Install `epel-release` then `restic` (or a static restic binary).
- Docker CE from the CentOS docker-ce repo works on Rocky 9.

## SELinux

| Guest | Observed mode | Notes |
| --- | --- | --- |
| Fedora 43 cloud | Permissive | Image default; harness green. |
| Rocky 9 cloud | Enforcing by default | Under the **LXD agent**, Docker bridge NAT can fail. Harness was run with **Permissive**. |

This is treated as an **LXD-guest / nested virt** limitation for the validation
environment, not as a BackupLint code defect. On bare metal or a normally
labeled SSH login with `container-selinux`, Enforcing is the expected production
mode and was not fully proven in this LXD pass.

## Resource notes

- Production harness pulls multiple app images (Nextcloud, databases, media apps).
- LXD guests need about **4 GiB RAM** and **~20 GiB** disk; 2 GiB guests can OOM mid-harness.
- Raspberry Pi 4 Model B (~3.7 GiB + swap) completed the full v0.2 suite over SSH.
- Raspberry Pi 3 Model B (~905 MiB + existing zram) completed the same suite without
  persistent swap changes.
- Raspberry Pi 3B+ was not separately timed; expect the same OS/package baseline.
- Run one heavy guest at a time on a ~14 GiB host.

## Verdict

x86_64 (Debian 12, Fedora 43, Rocky 9 with Python 3.11) and **ARM64** (Raspberry Pi 4
Model B and Raspberry Pi 3 Model B on Raspberry Pi OS / Debian 13) completed the v0.2
production and integrity campaigns. Raspberry Pi 3B+ is expected on that baseline
without a separate benchmark in this campaign.

WSL2 remains deferred.
