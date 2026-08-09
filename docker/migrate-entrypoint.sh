#!/bin/sh
# Entrypoint for the `migrator` image target.
#
# Two steps, in this order, because the second depends on the first:
#   1. ensure the least-privilege application role exists (and its password matches
#      the current secret)
#   2. run the Alembic migrations
#
# Step 1 is here because migration 20260802_05_tenant_rls GRANTs to APP_DATABASE_ROLE
# but never CREATEs it — docker/init-db.sh does that, and it only ever runs as a
# Postgres container init hook. On RDS there is no such hook, so without this the
# grants fail against a role that does not exist.
set -eu

python scripts/bootstrap_db_role.py

exec "$@"
