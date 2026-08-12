#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${project_dir}"

if [[ ! -f .env ]]; then
  printf '.env fehlt. Kopiere .env.production.example nach .env und ändere alle Geheimnisse.\n' >&2
  exit 2
fi
if grep -q 'replace-with-' .env; then
  printf '.env enthält noch Platzhalter. Produktionsstart abgebrochen.\n' >&2
  exit 2
fi

revision="$(git rev-parse HEAD 2>/dev/null || printf 'unknown')"
if grep -q '^DEPLOY_REVISION=' .env; then
  sed -i.bak "s/^DEPLOY_REVISION=.*/DEPLOY_REVISION=${revision}/" .env
  rm -f .env.bak
else
  printf '\nDEPLOY_REVISION=%s\n' "${revision}" >> .env
fi

docker compose -f compose.yaml -f compose.prod.yaml config --quiet
docker compose -f compose.yaml -f compose.prod.yaml up -d --build --remove-orphans --wait
docker compose -f compose.yaml -f compose.prod.yaml exec -T web python manage.py check
docker compose -f compose.yaml -f compose.prod.yaml ps
