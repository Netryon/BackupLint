#!/usr/bin/env bash
# Prepare an LXD platform VM: launch, network, prereqs, repo copy, snapshot, stop.
# Does NOT run the full validation harness.
#
# Usage:
#   ./scripts/prepare-ready-vm.sh <name> <image> <family> [ipv4-host-octet]
set -euo pipefail

NAME="${1:?vm name}"
IMAGE="${2:?lxd image}"
FAMILY="${3:?debian|fedora|rhel}"
IPV4_HOST="${4:-10}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARBALL="/tmp/backuplint-src.tar.gz"
GW="10.150.21.1"
GUEST_IP="10.150.21.${IPV4_HOST}"

log() { printf '%s\n' "$*" >&2; }

pack_repo() {
  tar -C "$ROOT" -czf "$TARBALL" \
    --exclude='.venv' \
    --exclude='.git' \
    --exclude='**/__pycache__' \
    --exclude='.pytest_cache' \
    --exclude='.ruff_cache' \
    --exclude='docs/demo/fixture' \
    --exclude='test-lab/**/pgdata' \
    --exclude='test-lab/**/data' \
    --exclude='test-lab/production/results.tsv' \
    --exclude='docs/platform-reports' \
    .
}

ensure_vm() {
  if lxc info "$NAME" >/dev/null 2>&1; then
    log "VM $NAME already exists — starting"
    lxc start "$NAME" 2>/dev/null || true
  else
    log "Launching $NAME from $IMAGE"
    lxc launch "$IMAGE" "$NAME" --vm \
      -c limits.cpu=2 \
      -c limits.memory=4GiB \
      -d root,size=25GiB
  fi
  for _ in $(seq 1 60); do
    if lxc exec "$NAME" -- true 2>/dev/null; then
      return 0
    fi
    sleep 5
  done
  log "ERROR: agent not ready for $NAME"
  exit 1
}

fix_guest_net() {
  log "Configuring guest network $GUEST_IP via $GW"
  local net_script
  net_script="$(mktemp)"
  cat >"$net_script" <<EOF
#!/bin/bash
set -euo pipefail
sleep 2
IFACE=\$(ip -o link show | awk -F': ' '\$2 ~ /^e/ {print \$2; exit}')
ip addr flush dev "\$IFACE" 2>/dev/null || true
ip addr add ${GUEST_IP}/24 dev "\$IFACE" 2>/dev/null || true
ip link set "\$IFACE" up
ip route replace default via ${GW} 2>/dev/null || true
rm -f /etc/resolv.conf
printf 'nameserver ${GW}\\nnameserver 8.8.8.8\\n' >/etc/resolv.conf
EOF
  chmod +x "$net_script"
  lxc file push "$net_script" "$NAME/usr/local/sbin/backuplint-net.sh"
  rm -f "$net_script"
  lxc exec "$NAME" -- chmod +x /usr/local/sbin/backuplint-net.sh
  lxc exec "$NAME" -- /usr/local/sbin/backuplint-net.sh
  # Persist across reboot
  lxc exec "$NAME" -- bash -c '
set -euo pipefail
cat >/etc/systemd/system/backuplint-net.service <<UNIT
[Unit]
Description=BackupLint static guest networking
After=network-pre.target
Before=network.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/backuplint-net.sh
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable backuplint-net.service
'
  lxc exec "$NAME" -- python3 -c 'import socket; print(socket.gethostbyname("example.com"))'
}

install_prereqs() {
  case "$FAMILY" in
    debian)
      lxc exec "$NAME" -- bash -c '
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1 && command -v restic >/dev/null; then
  echo "prereqs already present"
  docker --version; docker compose version; restic version | head -1
  exit 0
fi
apt-get update -qq
apt-get install -y -qq ca-certificates curl git gnupg python3 python3-venv python3-pip restic
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --batch --yes --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $VERSION_CODENAME stable" \
  >/etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable --now docker
user=$(getent passwd | awk -F: "\$3>=1000 && \$3<65534 {print \$1; exit}")
usermod -aG docker "$user"
docker --version; docker compose version; restic version | head -1
'
      ;;
    fedora)
      lxc exec "$NAME" -- bash -c '
set -euo pipefail
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1 && command -v restic >/dev/null; then
  echo "prereqs already present"
  docker --version; docker compose version; getenforce || true
  exit 0
fi
dnf -y install git python3 python3-pip python3-virtualenv restic dnf-plugins-core curl
curl -fsSL https://download.docker.com/linux/fedora/docker-ce.repo -o /etc/yum.repos.d/docker-ce.repo
dnf -y install docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable --now docker
user=$(getent passwd | awk -F: "\$3>=1000 && \$3<65534 {print \$1; exit}")
usermod -aG docker "$user"
docker --version; docker compose version; getenforce || true
'
      ;;
    rhel)
      lxc exec "$NAME" -- bash -c '
set -euo pipefail
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1 && command -v restic >/dev/null && command -v python3.11 >/dev/null; then
  echo "prereqs already present"
  docker --version; docker compose version; getenforce || true
  # Keep Permissive for LXD harness compatibility
  setenforce 0 2>/dev/null || true
  sed -i "s/^SELINUX=enforcing/SELINUX=permissive/" /etc/selinux/config 2>/dev/null || true
  exit 0
fi
dnf -y install git python3 python3-pip curl ca-certificates
dnf -y install python3.11 python3.11-pip
dnf -y install epel-release
dnf -y install restic || {
  ver=0.17.3
  dnf -y install bzip2 >/dev/null 2>&1 || true
  curl -fsSL "https://github.com/restic/restic/releases/download/v${ver}/restic_${ver}_linux_amd64.bz2" \
    | bunzip2 > /usr/local/bin/restic
  chmod +x /usr/local/bin/restic
}
curl -fsSL https://download.docker.com/linux/centos/docker-ce.repo -o /etc/yum.repos.d/docker-ce.repo
dnf -y install docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable --now docker
user=$(getent passwd | awk -F: "\$3>=1000 && \$3<65534 {print \$1; exit}")
usermod -aG docker "$user"
setenforce 0 2>/dev/null || true
sed -i "s/^SELINUX=enforcing/SELINUX=permissive/" /etc/selinux/config 2>/dev/null || true
docker --version; docker compose version; getenforce || true; restic version | head -1
'
      ;;
    *) log "Unknown family $FAMILY"; exit 1 ;;
  esac
}

copy_repo() {
  USERNAME="$(lxc exec "$NAME" -- bash -c 'getent passwd | awk -F: "\$3>=1000 && \$3<65534 {print \$1; exit}"')"
  HOME_DIR="$(lxc exec "$NAME" -- bash -c "getent passwd $USERNAME | cut -d: -f6")"
  log "Guest user=$USERNAME home=$HOME_DIR — copying repo"
  lxc exec "$NAME" -- rm -f /tmp/backuplint-src.tar.gz
  lxc file push "$TARBALL" "$NAME/tmp/backuplint-src.tar.gz"
  lxc exec "$NAME" -- bash -c "
set -euo pipefail
rm -rf '$HOME_DIR/backuplint'
mkdir -p '$HOME_DIR/backuplint'
tar -xzf /tmp/backuplint-src.tar.gz -C '$HOME_DIR/backuplint'
chown -R '$USERNAME:$USERNAME' '$HOME_DIR/backuplint'
chmod +x '$HOME_DIR/backuplint/scripts/'*.sh || true
"
}

snapshot_and_stop() {
  lxc snapshot "$NAME" platform-ready --reuse 2>/dev/null || lxc snapshot "$NAME" platform-ready
  log "Snapshot $NAME/platform-ready taken"
  # Smoke check
  lxc exec "$NAME" -- bash -c 'docker --version; docker compose version; restic version | head -1; python3 --version; command -v python3.11 >/dev/null && python3.11 --version || true'
  lxc stop "$NAME"
  log "STOPPED $NAME (ready for next use)"
}

main() {
  pack_repo
  ensure_vm
  fix_guest_net
  install_prereqs
  copy_repo
  snapshot_and_stop
  log "DONE prepare $NAME"
}

main "$@"
