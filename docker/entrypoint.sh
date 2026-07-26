#!/bin/bash
set -e

# Run migrations
alembic -c migrations/alembic.ini upgrade head

# Start App
exec "$@"
