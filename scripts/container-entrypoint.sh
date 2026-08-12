#!/usr/bin/env sh
set -eu

mkdir -p /app/media /app/staticfiles /app/celerybeat
chown -R app:app /app/media /app/staticfiles /app/celerybeat
exec gosu app "$@"
