#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target_ref="${1:-origin/main}"
compose=(docker compose -f "${project_dir}/compose.yaml" -f "${project_dir}/compose.prod.yaml")
cd "${project_dir}"

if [[ ! -f .env ]]; then
  printf '.env fehlt. Update abgebrochen.\n' >&2
  exit 2
fi
if [[ -n "$(git status --porcelain)" ]]; then
  printf 'Arbeitsverzeichnis enthält lokale Änderungen. Update abgebrochen.\n' >&2
  exit 2
fi

previous_revision="$(git rev-parse HEAD)"
printf 'Sicherung vor dem Update ...\n'
"${project_dir}/scripts/backup.sh"
git fetch --tags origin
git checkout --detach "${target_ref}"
new_revision="$(git rev-parse HEAD)"

if grep -q '^DEPLOY_REVISION=' .env; then
  sed -i.bak "s/^DEPLOY_REVISION=.*/DEPLOY_REVISION=${new_revision}/" .env
  rm -f .env.bak
else
  printf '\nDEPLOY_REVISION=%s\n' "${new_revision}" >> .env
fi

if ! "${compose[@]}" up -d --build --remove-orphans --wait; then
  printf 'Update fehlgeschlagen. Quellcode wird auf %s zurückgesetzt.\n' "${previous_revision}" >&2
  git checkout --detach "${previous_revision}"
  "${compose[@]}" up -d --build --remove-orphans
  exit 1
fi

"${compose[@]}" exec -T web python manage.py check
printf 'Update abgeschlossen: %s\n' "${new_revision}"
