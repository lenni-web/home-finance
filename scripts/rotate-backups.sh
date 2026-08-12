#!/usr/bin/env bash
set -euo pipefail

backup_dir="${1:-${BACKUP_DIR:-$(pwd)/backups}}"
daily_days="${BACKUP_RETENTION_DAYS:-7}"
monthly_copies="${BACKUP_MONTHLY_COPIES:-6}"

if [[ ! -d "${backup_dir}" ]]; then
  exit 0
fi
if ! [[ "${daily_days}" =~ ^[0-9]+$ && "${monthly_copies}" =~ ^[0-9]+$ ]]; then
  printf 'Ungültige Aufbewahrungswerte.\n' >&2
  exit 2
fi

backups=()
while IFS= read -r file; do
  backups+=("${file}")
done < <(find "${backup_dir}" -maxdepth 1 -type f \
  \( -name 'home-finance-*.tar.gz' -o -name 'home-finance-*.tar.gz.gpg' \) -print | sort -r)

keep=()
is_kept() {
  local candidate="$1" saved
  for saved in "${keep[@]:-}"; do
    [[ "${saved}" == "${candidate}" ]] && return 0
  done
  return 1
}
now="$(date +%s)"
for file in "${backups[@]}"; do
  modified="$(stat -c %Y "${file}" 2>/dev/null || stat -f %m "${file}")"
  age_days="$(( (now - modified) / 86400 ))"
  if (( age_days < daily_days )); then
    keep+=("${file}")
  fi
done

months=()
month_count=0
for file in "${backups[@]}"; do
  base="$(basename "${file}")"
  month="${base:13:6}"
  month_seen=0
  for saved_month in "${months[@]:-}"; do
    [[ "${saved_month}" == "${month}" ]] && month_seen=1
  done
  if [[ "${month_seen}" == "0" && ${month_count} -lt ${monthly_copies} ]]; then
    months+=("${month}")
    is_kept "${file}" || keep+=("${file}")
    month_count=$((month_count + 1))
  fi
done

for file in "${backups[@]}"; do
  if ! is_kept "${file}"; then
    rm -f -- "${file}"
    printf 'Altes Backup entfernt: %s\n' "${file}"
  fi
done
