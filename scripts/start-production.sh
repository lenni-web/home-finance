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

docker compose -f compose.yaml -f compose.prod.yaml up -d --build
docker compose -f compose.yaml -f compose.prod.yaml ps
