#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
backup_dir="${BACKUP_DIR:-${project_dir}/backups}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="${backup_dir}/home-finance-${timestamp}.tar.gz"
temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/home-finance-backup.XXXXXX")"
compose=(docker compose -f "${project_dir}/compose.yaml" -f "${project_dir}/compose.prod.yaml")

checksum() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$@"
  else
    shasum -a 256 "$@"
  fi
}

cleanup() {
  rm -rf "${temp_dir}"
}
trap cleanup EXIT

mkdir -p "${backup_dir}"
chmod 700 "${backup_dir}"

"${compose[@]}" exec -T db sh -c \
  'pg_dump --clean --if-exists --no-owner --no-privileges -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  > "${temp_dir}/database.sql"
"${compose[@]}" exec -T web tar -C /app/media -cf - . > "${temp_dir}/documents.tar"

if [[ -f "${project_dir}/.env" ]]; then
  cp "${project_dir}/.env" "${temp_dir}/environment.env"
  chmod 600 "${temp_dir}/environment.env"
fi

git_revision="$(git -C "${project_dir}" rev-parse HEAD 2>/dev/null || printf 'unknown')"
{
  printf 'created_utc=%s\n' "${timestamp}"
  printf 'git_revision=%s\n' "${git_revision}"
  printf 'format=home-finance-backup-v1\n'
} > "${temp_dir}/manifest.txt"

(
  cd "${temp_dir}"
  checksum database.sql documents.tar manifest.txt > SHA256SUMS
  if [[ -f environment.env ]]; then
    checksum environment.env >> SHA256SUMS
  fi
  tar -czf "${archive}" .
)
chmod 600 "${archive}"
printf 'Backup erstellt: %s\n' "${archive}"
printf 'Wichtig: Das Archiv enthält Finanzdaten und ggf. die .env-Konfiguration.\n'
