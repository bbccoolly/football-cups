#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  sudo bash scripts/linux/prepare-data-disk.sh --device /dev/<disk>
  sudo bash scripts/linux/prepare-data-disk.sh --device /dev/<disk> \
    --confirm-device /dev/<disk> --apply

The default mode is read-only. Formatting requires --apply and an identical
--confirm-device value. The script refuses mounted, partitioned, or formatted disks.
EOF
}

device=""
confirmation=""
apply=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --device) device="${2:-}"; shift 2 ;;
    --confirm-device) confirmation="${2:-}"; shift 2 ;;
    --apply) apply=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "This script must run as root." >&2
  exit 2
fi
if [[ -z "${device}" || ! -b "${device}" ]]; then
  echo "--device must identify an existing block device." >&2
  exit 2
fi

device="$(readlink -f "${device}")"
echo "Candidate device: ${device}"
lsblk -o NAME,PATH,SIZE,TYPE,FSTYPE,MOUNTPOINTS,SERIAL "${device}"

if [[ "$(lsblk -dnro TYPE "${device}")" != "disk" ]]; then
  echo "The selected device is not a whole disk." >&2
  exit 3
fi
if [[ "$(lsblk -nrpo NAME "${device}" | wc -l)" -ne 1 ]]; then
  echo "The selected disk already has child partitions." >&2
  exit 3
fi
if lsblk -dnro MOUNTPOINTS "${device}" | grep -q '[^[:space:]]'; then
  echo "The selected disk is mounted." >&2
  exit 3
fi
if blkid "${device}" >/dev/null 2>&1; then
  echo "The selected disk already contains a filesystem signature." >&2
  exit 3
fi
if mountpoint -q /srv/football-cups 2>/dev/null; then
  echo "/srv/football-cups is already a mounted filesystem." >&2
  exit 3
fi
if [[ -d /srv/football-cups && -n "$(find /srv/football-cups -mindepth 1 -print -quit)" ]]; then
  echo "/srv/football-cups exists and is not empty." >&2
  exit 3
fi

if [[ "${apply}" != true ]]; then
  echo "Read-only validation passed. Re-run with --confirm-device ${device} --apply to format it."
  exit 0
fi
if [[ "$(readlink -f "${confirmation}" 2>/dev/null || true)" != "${device}" ]]; then
  echo "--confirm-device must resolve to exactly ${device}." >&2
  exit 2
fi
if ! id football-cups >/dev/null 2>&1; then
  echo "Run bootstrap-smoke.sh --apply before preparing the data disk." >&2
  exit 3
fi

mkfs.ext4 -L football-data "${device}"
uuid="$(blkid -s UUID -o value "${device}")"
install -d -o root -g root -m 0755 /srv/football-cups
if ! grep -q "^UUID=${uuid}[[:space:]]" /etc/fstab; then
  echo "UUID=${uuid} /srv/football-cups ext4 defaults,noatime 0 2" >> /etc/fstab
fi
mount /srv/football-cups
mountpoint -q /srv/football-cups
install -d -o football-cups -g football-cups -m 0750 \
  /srv/football-cups/data/500 \
  /srv/football-cups/backup-staging \
  /srv/football-cups/restore-test
install -d -o root -g root -m 0700 /srv/football-cups/postgresql/17-main
findmnt /srv/football-cups
echo "Data disk prepared. Reboot and verify findmnt before installing timers."
