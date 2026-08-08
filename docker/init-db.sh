#!/bin/sh
set -eu

if [ -z "${APP_DATABASE_USER:-}" ] || [ -z "${APP_DATABASE_PASSWORD:-}" ]; then
  exit 0
fi

escaped_user=$(printf '%s' "$APP_DATABASE_USER" | sed 's/"/""/g')
escaped_password=$(printf '%s' "$APP_DATABASE_PASSWORD" | sed "s/'/''/g")
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -c "CREATE ROLE \"$escaped_user\" LOGIN PASSWORD '$escaped_password'"
