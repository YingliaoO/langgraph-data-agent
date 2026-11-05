#!/bin/bash
set -euo pipefail

wait_for_mysql() {
  python - <<'PYBLOCK'
import os
import time
from urllib.parse import urlparse

import pymysql

database_url = os.getenv("DATABASE_URL", "").strip()
if not database_url or database_url.startswith("sqlite"):
    print("[backend] DATABASE_URL indicates SQLite or is unset; skipping MySQL wait.")
    raise SystemExit(0)

parsed = urlparse(database_url)
host = parsed.hostname or os.getenv("MYSQL_HOST", "mysql")
port = parsed.port or int(os.getenv("MYSQL_PORT", "3306"))
user = parsed.username or os.getenv("MYSQL_USER") or "root"
password = parsed.password or os.getenv("MYSQL_PASSWORD") or os.getenv("MYSQL_ROOT_PASSWORD") or ""
db_name = (parsed.path or "").lstrip("/") or os.getenv("MYSQL_DATABASE") or None

deadline = time.monotonic() + int(os.getenv("MYSQL_WAIT_SECONDS", "180"))
print(f"[backend] Waiting for MySQL at {host}:{port} (db={db_name or '<default>'}) ...")

while time.monotonic() < deadline:
    try:
        conn = pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=db_name,
            connect_timeout=5,
            read_timeout=5,
            write_timeout=5,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.close()
        print("[backend] MySQL is ready.")
        break
    except Exception:
        time.sleep(2)
else:
    raise SystemExit(f"MySQL is not reachable at {host}:{port} within timeout")
PYBLOCK
}

wait_for_mysql

exec "$@"
