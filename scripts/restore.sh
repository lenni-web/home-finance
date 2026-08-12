#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || "$1" != "--yes" ]]; then
  printf 'Verwendung: %s --yes /absoluter/pfad/backup.tar.gz\n' "$0" >&2
  printf 'Achtung: Datenbank und Dokumentarchiv werden vollständig ersetzt.\n' >&2
  exit 2
fi

archive="$2"
if [[ ! -f "${archive}" ]]; then
  printf 'Backup nicht gefunden: %s\n' "${archive}" >&2
  exit 2
fi

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/home-finance-restore.XXXXXX")"
plain_archive="${archive}"
compose=(docker compose -f "${project_dir}/compose.yaml" -f "${project_dir}/compose.prod.yaml")
services_stopped=0

verify_checksums() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum --check SHA256SUMS
  else
    shasum -a 256 --check SHA256SUMS
  fi
}

cleanup() {
  rm -rf "${temp_dir}"
  if [[ "${services_stopped}" == "1" ]]; then
    "${compose[@]}" up -d web worker beat >/dev/null || true
  fi
}
trap cleanup EXIT

if [[ "${archive}" == *.gpg ]]; then
  if ! command -v gpg >/dev/null 2>&1; then
    printf 'Verschlüsseltes Backup benötigt gpg.\n' >&2
    exit 1
  fi
  plain_archive="${temp_dir}/backup.tar.gz"
  gpg --batch --output "${plain_archive}" --decrypt "${archive}"
fi

if tar -tzf "${plain_archive}" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
  printf 'Unsichere Pfade im Backup erkannt. Abbruch.\n' >&2
  exit 1
fi
tar -xzf "${plain_archive}" -C "${temp_dir}"

for required in database.sql documents.tar manifest.txt SHA256SUMS; do
  if [[ ! -f "${temp_dir}/${required}" ]]; then
    printf 'Ungültiges Backup: %s fehlt.\n' "${required}" >&2
    exit 1
  fi
done
(
  cd "${temp_dir}"
  verify_checksums
)

"${compose[@]}" stop web worker beat
services_stopped=1
"${compose[@]}" up -d --wait db
"${compose[@]}" exec -T db sh -c \
  'dropdb --if-exists -U "$POSTGRES_USER" "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
"${compose[@]}" exec -T db sh -c \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  < "${temp_dir}/database.sql"
"${compose[@]}" run --rm -T --no-deps web sh -c \
  'find /app/media -mindepth 1 -delete'
"${compose[@]}" run --rm -T --no-deps web tar -C /app/media -xf - \
  < "${temp_dir}/documents.tar"

"${compose[@]}" up -d web worker beat
services_stopped=0
printf 'Wiederherstellung abgeschlossen. Gesicherte environment.env wurde nicht automatisch eingespielt.\n'
