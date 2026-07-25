#!/bin/bash
set -e

# Run migrations
alembic upgrade head

# Start App
exec "$@"
