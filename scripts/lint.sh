#!/bin/sh
# Lint and format check, run inside the api container. Pass `--fix` to autofix.
set -eu
cd "$(dirname "$0")/.."
if [ "${1:-}" = "--fix" ]; then
    exec docker compose -f docker/docker-compose.yml run --rm --no-deps api sh -c "ruff check --fix . && ruff format ."
fi
exec docker compose -f docker/docker-compose.yml run --rm --no-deps api sh -c "ruff check . && ruff format --check ."
