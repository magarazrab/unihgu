#!/bin/sh
set -e
PORT="${PORT:-8080}"
echo "Starting HGU Test on port ${PORT}"
echo "DATABASE_URL set: $([ -n \"$DATABASE_URL\" ] && echo yes || echo NO — connect Postgres!)"
exec gunicorn -b "0.0.0.0:${PORT}" -w 1 --timeout 120 --access-logfile - --error-logfile - app:app
