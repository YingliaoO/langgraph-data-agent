from __future__ import annotations

import json
import mimetypes
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BASE_URL = "http://localhost:8000"


def run(
    args: list[str],
    *,
    input_text: str | None = None,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    kwargs = {
        "cwd": str(ROOT),
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "check": False,
    }
    if input_text is not None:
        kwargs["input"] = input_text
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE

    result = subprocess.run(args, **kwargs)
    if check and result.returncode != 0:
        if capture:
            if result.stdout:
                print(result.stdout, file=sys.stderr)
            if result.stderr:
                print(result.stderr, file=sys.stderr)
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(args[:5])}"
        )
    return result


def container_env(service: str, name: str) -> str:
    result = run(
        ["docker", "compose", "exec", "-T", service, "printenv", name],
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def wait_ready(max_attempts: int = 45) -> None:
    last_error = ""
    for _ in range(max_attempts):
        try:
            with urllib.request.urlopen(f"{BASE_URL}/ready", timeout=10) as resp:
                if resp.status == 200:
                    body = json.loads(resp.read().decode("utf-8"))
                    print("/ready = 200", json.dumps(body, ensure_ascii=False))
                    return
        except Exception as exc:  # noqa: BLE001 - diagnostic retry boundary
            last_error = str(exc)
        time.sleep(2)
    raise RuntimeError(f"Backend did not become ready in time. Last error: {last_error}")


def mysql_cli_base() -> list[str]:
    root_password = container_env("mysql", "MYSQL_ROOT_PASSWORD")
    mysql_user = container_env("mysql", "MYSQL_USER")
    mysql_password = container_env("mysql", "MYSQL_PASSWORD")

    if root_password:
        user = "root"
        password = root_password
    elif mysql_user:
        user = mysql_user
        password = mysql_password
    else:
        user = "root"
        password = ""

    args = [
        "docker", "compose", "exec", "-T", "mysql",
        "mysql", "--default-character-set=utf8mb4", f"-u{user}",
    ]
    if password:
        args.append(f"--password={password}")
    return args


def discover_mysql_database() -> str:
    """Use the application database configured for the MySQL demo container."""
    configured = container_env("mysql", "MYSQL_DATABASE")
    if configured:
        return configured
    raise RuntimeError("MYSQL_DATABASE is not configured in the MySQL container")


def seed_mysql() -> None:
    sql = (HERE / "mysql_seed.sql").read_text(encoding="utf-8")
    db = discover_mysql_database()
    print(f"Using MySQL database: {db}")
    run(mysql_cli_base() + [db], input_text=sql)

    verify = run(
        mysql_cli_base()
        + [
            "-Nse",
            "SELECT COUNT(*) FROM retail_orders;",
            db,
        ],
        capture=True,
    )
    count = (verify.stdout or "").strip()
    if count != "12":
        raise RuntimeError(f"MySQL verification failed: expected 12 retail_orders, got {count!r}")
    print("MySQL seed verified: retail_orders = 12")


def neo4j_cli_base() -> list[str]:
    username = container_env("neo4j", "NEO4J_USERNAME")
    password = container_env("neo4j", "NEO4J_PASSWORD")
    auth = container_env("neo4j", "NEO4J_AUTH")

    args = ["docker", "compose", "exec", "-T", "neo4j", "cypher-shell"]

    if username and password:
        return args + ["-u", username, "-p", password]

    if auth and auth.lower() != "none" and "/" in auth:
        user, passwd = auth.split("/", 1)
        return args + ["-u", user, "-p", passwd]

    return args


def seed_neo4j() -> None:
    cypher = (HERE / "neo4j_seed.cypher").read_text(encoding="utf-8")
    run(neo4j_cli_base(), input_text=cypher)

    verify = run(
        neo4j_cli_base()
        + ["MATCH (p:RetailProduct) RETURN count(p) AS n;"],
        capture=True,
    )
    stdout = (verify.stdout or "").strip()
    numeric_lines = [line.strip() for line in stdout.splitlines() if line.strip().isdigit()]
    if not numeric_lines or numeric_lines[-1] != "6":
        raise RuntimeError(
            "Neo4j verification failed: expected 6 RetailProduct nodes. "
            f"cypher-shell output was: {stdout!r}"
        )
    print("Neo4j seed verified: RetailProduct = 6")


def multipart_upload(url: str, path: Path, timeout: int = 120) -> dict:
    boundary = "----MultiSourceDataAgentEnterprise" + uuid.uuid4().hex
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    file_bytes = path.read_bytes()
    data = b"".join(
        [
            f"--{boundary}\r\n".encode("ascii"),
            f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode("ascii"),
            f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
            file_bytes,
            b"\r\n",
            f"--{boundary}--\r\n".encode("ascii"),
        ]
    )
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_json(url: str, payload: dict, timeout: int = 360) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ingest_knowledge(wait_seconds: int = 25) -> None:
    upload = multipart_upload(
        f"{BASE_URL}/api/v1/upload/file",
        HERE / "knowledge_base.csv",
    )
    if not upload.get("success") or not upload.get("file_path"):
        raise RuntimeError(f"Knowledge upload failed: {upload}")

    response = post_json(
        f"{BASE_URL}/api/v1/chat/",
        {
            "message": "Import this CSV dataset",
            "session_id": "enterprise-retail-bootstrap",
            "stream": False,
            "file_path": upload["file_path"],
            "ingest_incremental": True,
        },
    )
    if response.get("route") != "file-query":
        raise RuntimeError(
            f"Expected file-query during knowledge ingestion, got {response.get('route')!r}"
        )

    print("Knowledge ingestion submitted through file-query in incremental mode.")
    if wait_seconds > 0:
        print(f"Waiting {wait_seconds}s for asynchronous knowledge ingestion...")
        time.sleep(wait_seconds)


def main() -> int:
    print("== Enterprise Retail Demo bootstrap v3 ==")

    print("\n[1/7] Checking Docker services...")
    run(["docker", "compose", "ps"])
    wait_ready()

    print("\n[2/7] Seeding MySQL retail_* tables...")
    seed_mysql()

    print("\n[3/7] Seeding Neo4j Retail* graph...")
    seed_neo4j()

    print("\n[4/7] Restarting backend so graph schema is refreshed...")
    run(["docker", "compose", "restart", "backend"])
    wait_ready()

    print("\n[5/7] Uploading enterprise knowledge through the real file-query path...")
    ingest_knowledge(wait_seconds=25)

    print("\n[6/7] Re-checking readiness...")
    wait_ready()

    print("\n[7/7] Bootstrap complete.")
    print(r"Next: python .\demo\enterprise_retail\smoke.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
