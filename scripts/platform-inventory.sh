#!/usr/bin/env bash
# Collect platform inventory for BackupLint compatibility matrix.
set -euo pipefail

os_name="$(. /etc/os-release && echo "$NAME")"
os_version="$(. /etc/os-release && echo "$VERSION_ID")"
arch="$(uname -m)"
kernel="$(uname -r)"
python_v="$(python3 --version 2>&1 || true)"
docker_v="$(docker --version 2>&1 || true)"
compose_v="$(docker compose version 2>&1 || true)"
restic_v="$(restic version 2>&1 | head -1 || true)"
fs_root="$(findmnt -no FSTYPE / 2>/dev/null || true)"
selinux="$(command -v getenforce >/dev/null && getenforce || echo N/A)"

cat <<EOF
OS_NAME=$os_name
OS_VERSION=$os_version
ARCH=$arch
KERNEL=$kernel
PYTHON=$python_v
DOCKER=$docker_v
COMPOSE=$compose_v
RESTIC=$restic_v
ROOT_FS=$fs_root
SELINUX=$selinux
EOF
