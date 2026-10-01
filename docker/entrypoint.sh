#!/bin/sh
set -e

# Wait for Postgres before migrate/gunicorn hammer the socket. Only relevant
# when POSTGRES_DB is set (SQLite needs no wait).
if [ -n "$POSTGRES_DB" ]; then
  echo "Waiting for Postgres at ${POSTGRES_HOST:-db}:${POSTGRES_PORT:-5432}..."
  python - <<'PYEOF' || true
import os, sys, time

import psycopg2

host = os.environ.get('POSTGRES_HOST', 'db')
port = os.environ.get('POSTGRES_PORT', '5432')
for attempt in range(60):
    try:
        psycopg2.connect(
            dbname=os.environ['POSTGRES_DB'],
            user=os.environ.get('POSTGRES_USER', 'postgres'),
            password=os.environ.get('POSTGRES_PASSWORD', ''),
            host=host, port=port, connect_timeout=3,
        ).close()
        sys.exit(0)
    except Exception:
        time.sleep(2)
print(f'Postgres at {host}:{port} unreachable after 120s', file=sys.stderr)
sys.exit(1)
PYEOF
fi

# Only the web container migrates and collects static files; the Celery
# worker/beat containers start against an already-migrated schema.
if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  echo "Running migrations..."
  python manage.py migrate --noinput
  echo "Collecting static files..."
  python manage.py collectstatic --noinput
fi

exec "$@"
