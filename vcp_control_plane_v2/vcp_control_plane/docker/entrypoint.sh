#!/bin/sh
# VCP Control Plane entrypoint
# Runs Alembic migrations then starts the application.
set -e

echo "==> Running database migrations..."
alembic upgrade head

echo "==> Starting VCP Control Plane..."
exec "$@"
