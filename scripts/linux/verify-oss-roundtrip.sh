#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  sudo -u football-cups bash scripts/linux/verify-oss-roundtrip.sh \
    --upload-root <oss-layout-dir> --remote-uri oss://<bucket>/<prefix> \
    --run-id <backup-run-id> --download-root <empty-dir> \
    --verify-target <empty-dir> [--apply]

The default mode is read-only. ossutil must already use an ECS RAM Role and the
Hangzhou internal endpoint. This script never accepts AccessKey values.
EOF
}

upload_root=""
remote_uri=""
run_id=""
download_root=""
verify_target=""
apply=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --upload-root) upload_root="${2:-}"; shift 2 ;;
    --remote-uri) remote_uri="${2:-}"; shift 2 ;;
    --run-id) run_id="${2:-}"; shift 2 ;;
    --download-root) download_root="${2:-}"; shift 2 ;;
    --verify-target) verify_target="${2:-}"; shift 2 ;;
    --apply) apply=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

if [[ -z "${upload_root}" || -z "${remote_uri}" || -z "${run_id}" \
   || -z "${download_root}" || -z "${verify_target}" ]]; then
  usage >&2
  exit 2
fi
if [[ "${remote_uri}" != oss://* ]]; then
  echo "--remote-uri must start with oss://" >&2
  exit 2
fi
if [[ ! "${run_id}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "--run-id contains unsupported characters." >&2
  exit 2
fi
if [[ ! -f "${upload_root}/runs/${run_id}/complete.json" ]]; then
  echo "The selected backup run has no complete marker." >&2
  exit 3
fi
for target in "${download_root}" "${verify_target}"; do
  if [[ -e "${target}" ]]; then
    if [[ -n "$(find "${target}" -mindepth 1 -print -quit)" ]]; then
      echo "Target must be empty: ${target}" >&2
      exit 3
    fi
  fi
done
if ! command -v ossutil >/dev/null 2>&1; then
  echo "ossutil is not installed." >&2
  exit 3
fi

if [[ "${apply}" != true ]]; then
  echo "Would sync ${upload_root}/ to ${remote_uri%/}/, download it to ${download_root}, and verify run ${run_id}."
  exit 0
fi

mkdir -p "${download_root}" "${verify_target}"
ossutil sync "${upload_root}/" "${remote_uri%/}/"
ossutil sync "${remote_uri%/}/" "${download_root}/"
FOOTBALL_CUPS_OSS_BACKUP_DIR="${download_root}" \
  /opt/football-cups/.venv/bin/football-cups-collector verify-oss-backup \
  --workspace /opt/football-cups --run-id "${run_id}" --target "${verify_target}"

echo "OSS upload, fresh download, and SHA-256 restore verification completed."
