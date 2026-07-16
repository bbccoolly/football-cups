#!/usr/bin/env bash
set -euo pipefail

enable=false
case "${1:-}" in
  "") ;;
  --enable) enable=true ;;
  -h|--help)
    echo "Usage: sudo bash scripts/linux/install-systemd.sh [--enable]"
    exit 0
    ;;
  *) exit 2 ;;
esac

if [[ "${EUID}" -ne 0 ]]; then
  echo "This script must run as root." >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ "${repo_root}" != "/opt/football-cups" ]]; then
  echo "Expected repository at /opt/football-cups; found ${repo_root}." >&2
  exit 2
fi
if ! mountpoint -q /srv/football-cups; then
  echo "/srv/football-cups is not a mounted filesystem." >&2
  exit 3
fi
if [[ ! -x /opt/football-cups/.venv/bin/football-cups-collector ]]; then
  echo "Collector virtual environment is not installed." >&2
  exit 3
fi
if [[ ! -r /etc/football-cups/collector.env ]]; then
  echo "/etc/football-cups/collector.env is missing or unreadable." >&2
  exit 3
fi

units=(
  football-cups-collector.service
  football-cups-collector.timer
  football-cups-db-import.service
  football-cups-db-import.timer
)
for unit in "${units[@]}"; do
  install -o root -g root -m 0644 "${repo_root}/scripts/linux/${unit}" "/etc/systemd/system/${unit}"
done

systemd-analyze verify "${units[@]/#//etc/systemd/system/}"
systemctl daemon-reload
if [[ "${enable}" == true ]]; then
  systemctl enable --now football-cups-collector.timer football-cups-db-import.timer
else
  echo "Units installed but timers remain disabled. Re-run with --enable only at formal cutover."
fi
