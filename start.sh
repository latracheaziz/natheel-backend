#!/bin/bash
set -e

# Ensure versions directory exists
mkdir -p alembic/versions

# Check if migrations exist by counting .py files
migration_count=$(find alembic/versions -maxdepth 1 -name "*.py" | wc -l)

if [ "$migration_count" -eq 0 ]; then
    echo "No migrations found, generating initial migration..."
    alembic revision --autogenerate -m "Initial migration"
fi

echo "Running migrations..."
alembic upgrade head

echo "Starting FastAPI server..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
