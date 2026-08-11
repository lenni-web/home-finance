#!/usr/bin/env sh
set -eu

mkdir -p /app/media /app/staticfiles
chown -R app:app /app/media /app/staticfiles
exec gosu app "$@"
