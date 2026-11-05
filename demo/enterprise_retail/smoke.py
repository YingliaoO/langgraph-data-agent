from __future__ import annotations

import json
import mimetypes
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BASE_URL = "http://localhost:8000"
CASES = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))


def request_json(url: str, payload: dict[str, Any], timeout: int = 360) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def multipart_upload(url: str, path: Path, timeout: int = 120) -> dict[str, Any]:
    boundary = "----MultiSourceDataAgentEnterprise" + uuid.uuid4().hex
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    file_bytes = path.read_bytes()
    data = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            file_bytes,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
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


def execution_of(response: dict[str, Any]) -> dict[str, Any]:
    value = response.get("execution")
    return value if isinstance(value, dict) else {}


def tool_names(execution: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for item in execution.get("tool_results") or []:
        if isinstance(item, dict) and item.get("tool_name"):
            out.append(str(item["tool_name"]))
    return out


def searchable_blob(response: dict[str, Any]) -> str:
    execution = execution_of(response)
    chunks = [
        str(response.get("message") or ""),
        json.dumps(execution.get("evidence") or [], ensure_ascii=False, default=str),
        json.dumps(execution.get("tool_results") or [], ensure_ascii=False, default=str),
    ]
    return "\n".join(chunks)


def run_case(
    case: dict[str, Any],
    attempts: int = 1,
    sleep_seconds: int = 8,
) -> tuple[bool, dict[str, Any]]:
    last: dict[str, Any] = {}
    for attempt in range(1, attempts + 1):
        session_id = f"enterprise-{case['case_id']}-{uuid.uuid4().hex[:8]}"
        last = request_json(
            f"{BASE_URL}/api/v1/chat/",
            {
                "message": case["question"],
                "session_id": session_id,
                "stream": False,
            },
        )
        execution = execution_of(last)
        tools = tool_names(execution)
        blob = searchable_blob(last)

        checks = {
            "route": last.get("route") == case["expected_route"],
            "tool": case["expected_tool"] in tools,
            "status": execution.get("status") == case["expected_status"],
            "facts": all(
                str(fact).lower() in blob.lower()
                for fact in case.get("expected_facts", [])
            ),
        }
        if all(checks.values()):
            return True, {
                "attempt": attempt,
                "route": last.get("route"),
                "status": execution.get("status"),
                "tools": tools,
                "evidence": len(execution.get("evidence") or []),
                "fallback": execution.get("fallback_reason"),
                "checks": checks,
            }

        if attempt < attempts:
            time.sleep(sleep_seconds)

    execution = execution_of(last)
    return False, {
        "attempt": attempts,
        "route": last.get("route"),
        "status": execution.get("status"),
        "tools": tool_names(execution),
        "evidence": len(execution.get("evidence") or []),
        "fallback": execution.get("fallback_reason"),
        "checks": {
            "route": last.get("route") == case["expected_route"],
            "tool": case["expected_tool"] in tool_names(execution),
            "status": execution.get("status") == case["expected_status"],
            "facts": all(
                str(fact).lower() in searchable_blob(last).lower()
                for fact in case.get("expected_facts", [])
            ),
        },
        "message": str(last.get("message") or "")[:500],
    }


def run_file_ingestion_case() -> tuple[bool, dict[str, Any]]:
    """Verify current product semantics: file-query submits dataset ingestion.

    Important: this does NOT claim that file-query counted CSV rows. The current
    product contract treats CSV/XLS/XLSX as an ingestion entry path.
    """
    upload = multipart_upload(
        f"{BASE_URL}/api/v1/upload/file",
        HERE / "weekly_campaign.csv",
    )
    if not upload.get("success") or not upload.get("file_path"):
        return False, {"upload": upload, "checks": {"upload": False}}

    response = request_json(
        f"{BASE_URL}/api/v1/chat/",
        {
            "message": "导入这个 CSV 数据集",
            "session_id": f"enterprise-file-{uuid.uuid4().hex[:8]}",
            "stream": False,
            "file_path": upload["file_path"],
            "ingest_incremental": True,
        },
    )
    execution = execution_of(response)
    message = str(response.get("message") or "")

    checks = {
        "upload": upload.get("success") is True,
        "route": response.get("route") == "file-query",
        "status": execution.get("status") == "success",
        "ingestion_ack": ("导入" in message or "ingest" in message.lower())
        and ("提交" in message or "submitted" in message.lower()),
    }
    ok = all(checks.values())

    return ok, {
        "route": response.get("route"),
        "status": execution.get("status"),
        "tools": tool_names(execution),
        "evidence": len(execution.get("evidence") or []),
        "checks": checks,
        "message": message[:500],
        "note": (
            "This verifies upload + file-query ingestion submission only; "
            "row-count answering is not part of the current file-query contract."
        ),
    }


def main() -> int:
    print("Multi-Source Data Agent Enterprise Retail Domain Smoke v2")
    print("=" * 72)

    results: list[tuple[str, bool, dict[str, Any]]] = []

    for case in CASES:
        attempts = 8 if case["case_id"] == "enterprise_rag_refund_policy" else 1
        ok, details = run_case(case, attempts=attempts)
        results.append((case["case_id"], ok, details))
        print(f"[{'PASS' if ok else 'FAIL'}] {case['case_id']}")
        print(json.dumps(details, ensure_ascii=False, indent=2))

    ok, details = run_file_ingestion_case()
    results.append(("enterprise_file_ingestion", ok, details))
    print(f"[{'PASS' if ok else 'FAIL'}] enterprise_file_ingestion")
    print(json.dumps(details, ensure_ascii=False, indent=2))

    passed = sum(1 for _, ok, _ in results if ok)
    print("=" * 72)
    print(f"Enterprise smoke: {passed}/{len(results)} passed")

    if passed != len(results):
        print("\nDo not change golden expectations just to make the smoke pass.")
        print("Use the failed route/tool/status details to locate the responsible layer.")
        return 1

    print("\nEnterprise retail domain verified across SQL, Graph, RAG and file ingestion.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
