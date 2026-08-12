#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  printf 'Dieses Installationsskript muss mit sudo ausgeführt werden.\n' >&2
  exit 2
fi

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
service_user="${SUDO_USER:-}"
if [[ -z "${service_user}" || "${service_user}" == "root" ]]; then
  printf 'Bitte mit sudo als der Benutzer ausführen, dem das Projekt gehört.\n' >&2
  exit 2
fi
if [[ ! -f "${project_dir}/.env" ]]; then
  printf '.env fehlt. Installation abgebrochen.\n' >&2
  exit 2
fi

install_unit() {
  source="$1"
  target="/etc/systemd/system/$(basename "${source}")"
  sed -e "s|__PROJECT_DIR__|${project_dir}|g" \
      -e "s|__SERVICE_USER__|${service_user}|g" "${source}" > "${target}"
  chmod 644 "${target}"
}

install_unit "${project_dir}/deploy/home-finance.service"
install_unit "${project_dir}/deploy/home-finance-backup.service"
install_unit "${project_dir}/deploy/home-finance-backup.timer"
systemctl daemon-reload
systemctl enable --now home-finance.service
systemctl enable --now home-finance-backup.timer
printf 'Produktionsdienste installiert. Timer prüfen mit: systemctl list-timers home-finance-backup.timer\n'
