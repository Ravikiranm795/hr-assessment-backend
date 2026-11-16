#!/bin/sh
# Run pytest in the api container against the disposable test-db. Args pass through, e.g.
#   scripts/test.sh tests/core -q
set -eu
cd "$(dirname "$0")/.."
if [ "$#" -eq 0 ]; then set -- tests -q; fi
exec docker compose -f docker/docker-compose.yml run --rm api pytest "$@"
