#!/bin/sh
# Container entrypoint.
#   * Server start (command is `uvicorn ...`): wait for the database, migrate when the
#     project has migrations, create the default super admin when that command exists,
#     then exec the server.
#   * Any other command (pytest, the worker, ad-hoc CLI): exec it untouched.
set -eu

if [ "${1:-}" != "uvicorn" ]; then
    exec "$@"
fi

python - <<'PY'
import asyncio, os, sys, time

import asyncpg

# Migrations may use their own (schema owner) login; fall back to the runtime URL.
dsn = (os.environ.get("DATABASE_URL_MIGRATIONS") or os.environ["DATABASE_URL"]).replace("+asyncpg", "", 1)
deadline = time.monotonic() + int(os.environ.get("DB_WAIT_TIMEOUT_SEC", "60"))


async def ping() -> None:
    conn = await asyncpg.connect(dsn, timeout=3)
    await conn.close()


while True:
    try:
        asyncio.run(ping())
        break
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready yet"
        if time.monotonic() > deadline:
            sys.exit(f"database not reachable: {exc}")
        print("waiting for database...", flush=True)
        time.sleep(1)
PY

if [ -f alembic.ini ]; then
    alembic upgrade head
fi

if python -m app.cli --help 2>/dev/null | grep -q "create-default-superadmin"; then
    python -m app.cli create-default-superadmin
fi

exec "$@"
