#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: sudo bash scripts/linux/install-postgresql.sh [--apply]"
  echo "Without --apply, the script only reports the planned installation."
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
if ! mountpoint -q /srv/football-cups; then
  echo "/srv/football-cups is not a mounted filesystem." >&2
  exit 3
fi

if [[ "${apply}" != true ]]; then
  cat <<'EOF'
Would:
  - configure the official PostgreSQL PGDG repository
  - disable package-time creation of a cluster on the system disk
  - install PostgreSQL 17
  - create 17/main at /srv/football-cups/postgresql/17-main
  - bind PostgreSQL to 127.0.0.1 and apply the 4 GiB memory profile
No application role, password, database, or DATABASE_URL is created.
EOF
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl postgresql-common
install -d -o root -g root -m 0755 /usr/share/postgresql-common/pgdg
curl --fail --show-error --location \
  -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
  https://www.postgresql.org/media/keys/ACCC4CF8.asc
printf 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt %s-pgdg main\n' \
  "${VERSION_CODENAME}" > /etc/apt/sources.list.d/pgdg.list
install -d -o root -g root -m 0755 /etc/postgresql-common
printf 'create_main_cluster = false\n' > /etc/postgresql-common/createcluster.conf
apt-get update
apt-get install -y postgresql-17

data_dir=/srv/football-cups/postgresql/17-main
cluster_line="$(pg_lsclusters --no-header 17 main 2>/dev/null || true)"
if [[ -n "${cluster_line}" ]]; then
  configured_dir="$(awk '{print $6}' <<<"${cluster_line}")"
  if [[ "${configured_dir}" != "${data_dir}" ]]; then
    echo "PostgreSQL 17/main already exists at ${configured_dir}; refusing to move it." >&2
    exit 3
  fi
else
  install -d -o postgres -g postgres -m 0700 "${data_dir}"
  pg_createcluster 17 main --datadir="${data_dir}" --start-conf=auto -- \
    --auth-local=peer --auth-host=scram-sha-256
fi

pg_conftool 17 main set listen_addresses 127.0.0.1
pg_conftool 17 main set shared_buffers 512MB
pg_conftool 17 main set effective_cache_size 2GB
pg_conftool 17 main set work_mem 8MB
pg_conftool 17 main set maintenance_work_mem 128MB
pg_conftool 17 main set max_connections 20
systemctl enable postgresql.service
pg_ctlcluster 17 main restart
pg_lsclusters 17 main

cat <<'EOF'
PostgreSQL 17 is running on the data disk. Create the football_cups role with
createuser --pwprompt and create its database interactively. Store the password
only in a mode-0600 pgpass file or another local secret service.
EOF
