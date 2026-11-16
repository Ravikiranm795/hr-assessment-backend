#!/bin/sh
# Fails if the default product or vendor name appears anywhere except app/core/config.py.
# The names are read from config.py, so this script holds no literal copy of them.
# docs/spec and docs/superpowers are the planning inputs and are exempt; generated docs are checked.
set -eu
cd "$(dirname "$0")/.."

status=0
for const in DEFAULT_APP_NAME DEFAULT_VENDOR_NAME; do
    value=$(sed -n "s/^${const} = \"\(.*\)\"$/\1/p" app/core/config.py)
    if [ -z "$value" ]; then
        echo "check_name: cannot read ${const} from app/core/config.py" >&2
        exit 2
    fi
    hits=$(grep -rnF --exclude-dir=__pycache__ --exclude-dir=spec --exclude-dir=superpowers \
        -- "$value" app tests docs | grep -v '^app/core/config.py:' || true)
    if [ -n "$hits" ]; then
        echo "check_name: ${const} value found outside app/core/config.py:" >&2
        echo "$hits" >&2
        status=1
    fi
done
[ "$status" -eq 0 ] && echo "check_name: ok"
exit "$status"
