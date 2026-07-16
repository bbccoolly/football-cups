#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: sudo bash scripts/linux/bootstrap-smoke.sh [--apply]"
  echo "Without --apply, the script only reports the actions it would take."
}

apply=false
case "${1:-}" in
  "") ;;
  --apply) apply=true ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac

if [[ "${EUID}" -ne 0 ]]; then
  echo "This script must run as root." >&2
  exit 2
fi

if [[ ! -r /etc/os-release ]]; then
  echo "Cannot identify the operating system." >&2
  exit 2
fi

# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "22.04" ]]; then
  echo "Expected Ubuntu 22.04; found ${ID:-unknown} ${VERSION_ID:-unknown}." >&2
  exit 2
fi

if [[ "${apply}" != true ]]; then
  cat <<'EOF'
Would:
  - update Ubuntu package metadata and install current upgrades
  - install Python 3.11 from the deadsnakes PPA plus build/runtime tools
  - create a 2 GiB /swapfile when no swap is active
  - create the football-cups system user and smoke/config directories
No repository clone, service installation, timer enablement, or reboot is performed.
EOF
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get upgrade -y
apt-get install -y ca-certificates curl git build-essential software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa
apt-get update
apt-get install -y python3.11 python3.11-dev python3.11-venv

if ! swapon --show=NAME --noheadings | grep -q .; then
  if [[ -e /swapfile ]]; then
    echo "/swapfile exists but is not active; refusing to replace it." >&2
    exit 3
  fi
  fallocate -l 2G /swapfile
  chmod 0600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

if ! getent group football-cups >/dev/null; then
  groupadd --system football-cups
fi
if ! id football-cups >/dev/null 2>&1; then
  useradd --system --gid football-cups --home-dir /var/lib/football-cups --create-home \
    --shell /usr/sbin/nologin football-cups
fi

install -d -o root -g football-cups -m 0750 /opt/football-cups
install -d -o root -g football-cups -m 0750 /etc/football-cups
install -d -o football-cups -g football-cups -m 0750 /var/lib/football-cups-smoke
install -d -o football-cups -g football-cups -m 0750 /var/lib/football-cups-smoke/500

python3.11 --version
swapon --show
echo "Smoke prerequisites are installed. Reboot if /var/run/reboot-required exists."
