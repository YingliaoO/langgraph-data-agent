import argparse
import datetime as dt
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


# Running ``python eval/run_eval.py`` makes ``eval/`` sys.path[0]. Add the
# repository root explicitly so project imports such as ``multisource_data_agent`` resolve
# without requiring callers to set PYTHONPATH manually.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.evaluators import evaluate_case, summarize_results
from eval.evaluators.fact_assertions import validate_fact_assertions
from eval.evaluators.rag_quality import validate_rag_case
from eval.evaluators.regression import diagnose_case, regression_gate, validate_error_expectations


DEFAULT_DATASET = Path(__file__).parent / "datasets" / "enterprise_retail_v1.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Multi-Source Data Agent end-to-end evaluation cases.")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="Path to JSONL evaluation cases.")
    parser.add_argument("--mode", choices=("chat", "retrieval", "acceptance"), default="chat",
                        help="Chat is full Agent HTTP; retrieval compares kb_ingest dense/rerank and optional hybrid.")
    parser.add_argument("--retrieval-base-url", default="http://localhost:8100")
    parser.add_argument("--candidate-k", type=int, default=20)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--compare-hybrid", action="store_true", help="Also compare independent lexical+dense RRF candidates followed by rerank.")
    parser.add_argument("--corpus", type=Path, help="Fixed retrieval fixture; defaults to rag_documents_v1.")
    parser.add_argument("--acceptance-resume", type=Path,
                        help="Acceptance only: preserve valid rows from this run directory and measure missing scores once.")
    parser.add_argument("--split", choices=("dev", "test"), help="Optional RAG case split.")
    parser.add_argument("--base-url", default="http://localhost:8000", help="Backend base URL.")
    parser.add_argument("--output-dir", default=str(Path(__file__).parent / "results"))
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--user-id", default="eval_runner")
    parser.add_argument("--model-label", help="Optional manually recorded model name; does not change runtime configuration.")
    parser.add_argument("--provider-label", help="Optional provider name only (no URL or credential); does not change runtime configuration.")
    parser.add_argument("--case-id", action="append", help="Run only matching case id. Can be repeated.")
    parser.add_argument("--wait-ready-timeout", type=float, default=120.0, help="Seconds to wait for backend readiness before running cases.")
    parser.add_argument("--retry-count", type=int, default=0, help="Retries per case for transient transport failures. Core baseline defaults to 0 so failures remain visible.")
    parser.add_argument("--retry-backoff", type=float, default=2.0, help="Seconds to wait between retries.")
    parser.add_argument("--min-route-accuracy", type=float, default=None)
    parser.add_argument("--max-p95-latency-ms", type=float, default=None)
    parser.add_argument("--fail-on-regression", action="store_true",
                        help="Fail declared route/tool/status/evidence/assertion checks; literal mismatches remain review warnings.")
    parser.add_argument("--review-run", type=Path,
                        help="Review saved chat results with current gate rules; makes no HTTP/model requests and preserves original artifacts.")
    args = parser.parse_args()

    if (args.fail_on_regression or args.review_run) and args.mode != "chat":
        parser.error("--fail-on-regression and --review-run apply only to chat mode")
    if args.review_run:
        return review_saved_run(args.review_run, Path(args.output_dir), args)

    dataset_path = Path(args.dataset)
    cases = load_cases(dataset_path, args.case_id or [])
    if args.split:
        cases = [case for case in cases if case.get("rag_eval", {}).get("split") == args.split]
    if not cases:
        print(f"No cases found in {dataset_path}", file=sys.stderr)
        return 2

    if args.mode == "retrieval":
        from eval.evaluators.rag_retrieval import run_retrieval_comparison
        return run_retrieval_comparison(args, cases, dataset_path)

    if args.mode == "acceptance":
        from eval.evaluators.rag_acceptance import run_acceptance_calibration
        return run_acceptance_calibration(args, cases, dataset_path)

    incompatible = [case["id"] for case in cases if case.get("rag_eval", {}).get("http_compatible") is False]
    if incompatible:
        parser.error("Isolated retrieval-fixture cases cannot run on the product chat domain; "
                     "use --mode retrieval or select http_compatible cases: " + ", ".join(incompatible))

    if not wait_for_backend_ready(args.base_url, args.wait_ready_timeout):
        print(
            f"Backend is not ready after {args.wait_ready_timeout:.0f}s: {args.base_url}",
            file=sys.stderr,
        )
        return 2

    # Capture reproducibility metadata before creating result artifacts so
    # generated output files cannot make a clean baseline look dirty.
    environment_metadata = collect_environment_metadata(
        dataset_path=dataset_path,
        base_url=args.base_url,
        timeout=args.timeout,
        retry_count=args.retry_count,
        case_count=len(cases),
        model_label=args.model_label,
        provider_label=args.provider_label,
    )

    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S_%fZ")
    run_dir = Path(args.output_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    environment_metadata.update({
        "run_id": run_id,
        "selected_case_ids": [case["id"] for case in cases],
        "regression_gate_enabled": args.fail_on_regression,
    })
    environment_metadata["source_archive"] = archive_source_snapshot(run_dir, environment_metadata["source_snapshot"])
    write_json(run_dir / "environment.json", environment_metadata)

    results: List[Dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        case_id = case.get("id", f"case_{index}")
        print(f"[{index}/{len(cases)}] {case_id}: {case.get('query')}")
        response, latency_ms, error, latency_attempts, http_status = call_chat_api_with_retries(
            base_url=args.base_url,
            query=case["query"],
            user_id=f"{args.user_id}_{run_id}_{case_id}",
            trace_id=f"eval-{case_id}-{uuid.uuid4().hex[:8]}",
            timeout=args.timeout,
            retry_count=args.retry_count,
            retry_backoff=args.retry_backoff,
            file_path=case.get("file_path"),
            ingest_incremental=case.get("ingest_incremental"),
        )
        result = evaluate_case(
            case=case,
            response=response,
            latency_ms=latency_ms,
            error=error,
            latency_attempts=latency_attempts,
            http_status=http_status,
        )
        result["response_tool_results"] = extract_response_tool_results(response)
        # Persist the bounded, public contract for source/citation audits;
        # metadata.agent_state is deliberately not the result of record.
        result["response_execution"] = response.get("execution") if isinstance(response, dict) else None
        results.append(result)
        # Preserve every completed request if a later request is interrupted.
        with (run_dir / "cases.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps(result, ensure_ascii=False) + "\n")
        print_case_status(result)

    summary = summarize_results(results)
    environment_metadata["source_snapshot_after"] = collect_source_snapshot()
    environment_metadata["snapshot_unchanged"] = (
        environment_metadata["source_snapshot"]["sha256"] == environment_metadata["source_snapshot_after"]["sha256"]
        and environment_metadata["dataset_sha256"] == hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    )
    summary["snapshot_unchanged"] = environment_metadata["snapshot_unchanged"]
    summary["regression_gate"] = {"enabled": args.fail_on_regression, **regression_gate(results, summary["snapshot_unchanged"])}
    write_json(run_dir / "summary.json", summary)
    write_json(run_dir / "environment.json", environment_metadata)
    write_report(run_dir / "report.md", summary, results, dataset_path)

    print("")
    print(f"Report written to: {run_dir / 'report.md'}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not thresholds_passed(summary, args):
        return 1
    return 0


def load_cases(dataset_path: Path, selected_ids: Sequence[str]) -> List[Dict[str, Any]]:
    selected = set(selected_ids)
    cases: List[Dict[str, Any]] = []
    seen = set()
    with dataset_path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            case = json.loads(stripped)
            if case.get("enabled", True) is False:
                continue
            validate_fact_assertions(case.get("fact_assertions", []))
            if not isinstance(case.get("id"), str) or not case["id"].strip() or case["id"] in seen:
                raise ValueError(f"Case at line {line_number} requires a unique nonempty id")
            seen.add(case["id"])
            if selected and case.get("id") not in selected:
                continue
            if "query" not in case:
                raise ValueError(f"Case at line {line_number} is missing query")
            validate_rag_case(case)
            validate_error_expectations(case)
            cases.append(case)
    if selected - {case["id"] for case in cases}:
        raise ValueError("Unknown or disabled case IDs: " + ", ".join(sorted(selected - {case["id"] for case in cases})))
    return cases


def wait_for_backend_ready(base_url: str, timeout_seconds: float) -> bool:
    deadline = time.perf_counter() + timeout_seconds
    url = base_url.rstrip("/") + "/api/v1/chat/routes"
    last_error = ""
    while time.perf_counter() < deadline:
        try:
            request = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(request, timeout=5) as response:
                if 200 <= response.status < 500:
                    print(f"Backend ready: {url}")
                    return True
        except Exception as exc:
            last_error = repr(exc)
        time.sleep(2)
    if last_error:
        print(f"Last readiness error: {last_error}", file=sys.stderr)
    return False


def call_chat_api_with_retries(
    base_url: str,
    query: str,
    user_id: str,
    trace_id: str,
    timeout: float,
    retry_count: int,
    retry_backoff: float,
    file_path: Optional[str] = None,
    ingest_incremental: Optional[bool] = None,
) -> Tuple[Optional[Dict[str, Any]], float, Optional[str], List[Dict[str, Any]], Optional[int]]:
    started_at = time.perf_counter()
    last_response: Optional[Dict[str, Any]] = None
    last_error: Optional[str] = None
    last_http_status: Optional[int] = None
    attempts: List[Dict[str, Any]] = []
    for attempt in range(retry_count + 1):
        response, latency_ms, error, http_status = call_chat_api(
            base_url=base_url,
            query=query,
            user_id=user_id,
            trace_id=trace_id,
            timeout=timeout,
            file_path=file_path,
            ingest_incremental=ingest_incremental,
        )
        last_response = response
        last_error = error
        last_http_status = http_status
        attempts.append(
            {
                "attempt": attempt + 1,
                "latency_ms": round(latency_ms, 2),
                "success": error is None,
                "error": error,
                "http_status": http_status,
            }
        )
        if error is None:
            return response, (time.perf_counter() - started_at) * 1000, None, attempts, http_status
        if attempt < retry_count and is_transient_error(error, http_status):
            print(f"  transient error, retrying in {retry_backoff:.1f}s: {error}")
            time.sleep(retry_backoff)
            continue
        break
    return last_response, (time.perf_counter() - started_at) * 1000, last_error, attempts, last_http_status


def is_transient_error(error: str, http_status: Optional[int] = None) -> bool:
    if http_status in {429, 502, 503}:
        return True
    markers = (
        "RemoteDisconnected",
        "ConnectionAbortedError",
        "ConnectionResetError",
        "ConnectionRefusedError",
        "timed out",
        "TimeoutError",
    )
    return any(marker in error for marker in markers)


def call_chat_api(
    base_url: str,
    query: str,
    user_id: str,
    trace_id: str,
    timeout: float,
    file_path: Optional[str] = None,
    ingest_incremental: Optional[bool] = None,
) -> Tuple[Optional[Dict[str, Any]], float, Optional[str], Optional[int]]:
    url = base_url.rstrip("/") + "/api/v1/chat/"
    payload = {"message": query, "stream": False, "trace_id": trace_id}
    if file_path:
        payload["file_path"] = file_path
    if isinstance(ingest_incremental, bool):
        payload["ingest_incremental"] = ingest_incremental
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8", "Authorization": "Bearer " + os.getenv("MULTISOURCE_AGENT_ACCESS_TOKEN", "")},
        method="POST",
    )
    started_at = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            latency_ms = (time.perf_counter() - started_at) * 1000
            return json.loads(raw), latency_ms, None, response.status
    except urllib.error.HTTPError as exc:
        latency_ms = (time.perf_counter() - started_at) * 1000
        body = exc.read().decode("utf-8", errors="replace")
        return None, latency_ms, f"HTTP {exc.code}: {body[:500]}", exc.code
    except Exception as exc:
        latency_ms = (time.perf_counter() - started_at) * 1000
        return None, latency_ms, repr(exc), None


def print_case_status(result: Dict[str, Any]) -> None:
    metrics = result["metrics"]
    print(
        "  http={http} route={actual}/{expected} route_ok={route_ok} exec={execution_status} "
        "tools={tools} evidence={evidence} fallback={fallback} latency={latency}ms".format(
            http=result["actual"].get("http_status"),
            actual=result["actual"].get("route"),
            expected=result["expected"].get("route"),
            route_ok=metric_status(metrics.get("route_correct")),
            execution_status=result["actual"].get("execution_status"),
            tools=",".join(result["actual"].get("executed_tools") or []),
            evidence=result["actual"].get("evidence_count"),
            fallback=result.get("fallback_reason"),
            latency=result.get("latency_ms"),
        )
    )
    if result.get("error"):
        print(f"  error={result['error']}")
    diagnostics = result.get("diagnostics", {})
    print("  failures=" + ",".join(diagnostics.get("failure_reasons", []))
          + " warnings=" + ",".join(diagnostics.get("warnings", [])))


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    content = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    path.write_text(content + "\n", encoding="utf-8")


def extract_response_tool_results(response: Optional[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """Best-effort extraction of serialized tool results from a chat response."""
    if not isinstance(response, dict):
        return None

    found: List[Dict[str, Any]] = []
    seen = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            candidates = value.get("tool_results")
            if isinstance(candidates, dict):
                candidates = [candidates]
            if isinstance(candidates, list):
                for item in candidates:
                    if isinstance(item, dict):
                        marker = repr(sorted((key, repr(val)) for key, val in item.items()))
                        if marker not in seen:
                            seen.add(marker)
                            found.append(item)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(response)
    return found or None


def collect_source_snapshot(root: Path = REPO_ROOT) -> Dict[str, Any]:
    """Fingerprint reviewed source/dependency files, never runtime credentials.

    A commit plus git_dirty cannot identify uncommitted code. This explicit
    allowlist excludes .env, data, logs, reports and generated files; it records
    hashes only and does not claim the remote service runs the same snapshot.
    """
    paths = set()
    for folder in ("multisource_data_agent", "kb_ingest/kb_service", "eval/evaluators", "tests"):
        paths.update(path for path in (root / folder).rglob("*.py") if path.is_file() and not path.is_symlink())
    for folder in ("eval/datasets", "eval/fixtures"):
        paths.update(path for path in (root / folder).glob("*.jsonl") if path.is_file() and not path.is_symlink())
    for name in (
        "eval/run_eval.py", "eval/compare_runs.py", "scripts/seed_rag_eval.py", "pyproject.toml", "requirements.txt",
        "kb_ingest/requirements.txt", "uv.lock", "poetry.lock", "requirements.lock",
        "demo/enterprise_retail/mysql_seed.sql", "demo/enterprise_retail/neo4j_seed.cypher",
        "demo/enterprise_retail/knowledge_base.csv",
    ):
        path = root / name
        if path.is_file() and not path.is_symlink():
            paths.add(path)
    hashes = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(paths)}
    payload = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"sha256": hashlib.sha256(payload).hexdigest(), "file_count": len(hashes), "files": hashes}


def archive_source_snapshot(run_dir: Path, snapshot: Dict[str, Any], root: Path = REPO_ROOT) -> Dict[str, Any]:
    """Store recoverable allowlisted bytes, never .env, uploads or runtime dumps."""
    files = snapshot.get("files", {})
    approved = collect_source_snapshot(root)["files"]
    if not files or not set(files) <= set(approved):
        raise ValueError("Source archive requires a nonempty approved file manifest")
    archive = run_dir / "source_snapshot.zip"
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as target:
        for name, expected_hash in sorted(files.items()):
            path = root / name
            path.resolve().relative_to(root.resolve())
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != expected_hash:
                raise ValueError("Source changed before archive capture: " + name)
            target.writestr(name, data)
        target.writestr("source_manifest.json", json.dumps(snapshot, sort_keys=True))
    return {"path": archive.name, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "file_count": len(files)}


def collect_environment_metadata(
    *, dataset_path: Path, base_url: str, timeout: float, retry_count: int, case_count: int,
    model_label: Optional[str] = None, provider_label: Optional[str] = None,
) -> Dict[str, Any]:
    dataset_bytes = dataset_path.read_bytes()
    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "commit_hash": _git_output("rev-parse", "HEAD"),
        "git_dirty": bool(_git_output("status", "--porcelain")),
        "source_snapshot": collect_source_snapshot(),
        "dataset_path": str(dataset_path),
        "dataset_sha256": hashlib.sha256(dataset_bytes).hexdigest(),
        "case_count": case_count,
        "model_label": model_label,
        "provider_label": provider_label,
        "model_labels_source": "operator supplied; runtime configuration not inspected",
        "base_url": base_url,
        "request_timeout_seconds": timeout,
        "runner_retry_count": retry_count,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "package_versions": _package_versions(),
        "package_versions_scope": "Local Runner packages only; not remote container/provider versions.",
    }


def _package_versions() -> Dict[str, Optional[str]]:
    versions = {}
    for name in ("multisource-data-agent", "fastapi", "langgraph", "langchain-core", "langchain-openai", "openai", "sqlglot", "pytest", "httpx", "pydantic"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def review_saved_run(source: Path, output: Path, args: argparse.Namespace) -> int:
    """Gate-only review: retain recorded scores, never call a backend or LLM."""
    if output.resolve().is_relative_to(source.resolve()):
        raise ValueError("Review output must be outside the original run directory")
    environment = json.loads((source / "environment.json").read_text(encoding="utf-8"))
    results = [json.loads(line) for line in (source / "cases.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [row.get("id") for row in results]
    if not results or len(results) != environment.get("case_count") or len(set(ids)) != len(ids):
        raise ValueError("Cannot review an incomplete run or duplicate case IDs")
    if environment.get("selected_case_ids") is not None and set(ids) != set(environment["selected_case_ids"]):
        raise ValueError("Saved cases do not match the recorded selection")
    for row in results:
        statuses = row["actual"].get("tool_statuses", [])
        row["metrics"]["tool_non_error_completion"] = row["metrics"].get("tool_execution_ok")
        row["metrics"]["tool_all_success"] = all(s == "success" for s in statuses) if statuses else None
        code = row["actual"].get("http_status")
        row["metrics"]["http_error"] = isinstance(code, int) and not 200 <= code < 300
        row["diagnostics"] = diagnose_case(row)
    summary = summarize_results(results)
    snapshot = collect_source_snapshot()
    review_dir = output / ("gate_review_" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S_%fZ"))
    review_dir.mkdir(parents=True, exist_ok=False)
    archive = archive_source_snapshot(review_dir, snapshot)
    summary["snapshot_unchanged"] = environment.get("snapshot_unchanged") is True and snapshot["sha256"] == collect_source_snapshot()["sha256"]
    summary["regression_gate"] = {"enabled": args.fail_on_regression, **regression_gate(results, summary["snapshot_unchanged"])}
    originals = {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                 for name in ("cases.jsonl", "summary.json", "environment.json", "report.md") if (source / name).is_file()}
    payload = {
        "review_kind": "gate_only_no_http_requests", "original_run": str(source),
        "original_file_sha256": originals,
        "original_source_sha256": environment.get("source_snapshot", {}).get("sha256"),
        "review_source_snapshot": snapshot, "review_source_archive": archive,
        "summary": summary,
        "cases": [{"id": row["id"], **row["diagnostics"]} for row in results],
        "scope": "Recorded quality scores and request timings are retained; only gate rules and additive metric labels/counts are reviewed.",
    }
    write_json(review_dir / "review.json", payload)
    lines = ["# Saved-run Gate Review (no new HTTP requests)", "", f"Original run: `{source}`", "",
             f"Gate: **{summary['regression_gate']['status']}**", "",
             "Original scores/timings were not rewritten. This is not a new Agent measurement.", "",
             "| Case | Check | Failures | Warnings |", "| --- | --- | --- | --- |"]
    lines += ["| {} | {} | {} | {} |".format(md_escape(row["id"]), row["diagnostics"]["check_status"],
              md_escape(", ".join(row["diagnostics"]["failure_reasons"]) or "none"),
              md_escape(", ".join(row["diagnostics"]["warnings"]) or "none")) for row in results]
    (review_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Gate-only review written to: {review_dir}; status={summary['regression_gate']['status']}")
    return 0 if thresholds_passed(summary, args) else 1


def _git_output(*args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except OSError:
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def write_report(
    path: Path,
    summary: Dict[str, Any],
    results: Sequence[Dict[str, Any]],
    dataset_path: Path,
) -> None:
    formal_metrics = (
        "http_success_rate",
        "agent_completion_rate",
        "route_accuracy",
        "tool_selection_accuracy",
        "tool_exact_match",
        "execution_status_accuracy",
        "tool_non_error_completion_rate",
        "tool_all_success_rate",
        "evidence_available_accuracy",
        "expected_fact_supported_by_evidence_rate",
        "asserted_fact_coverage",
        "asserted_facts_ok_rate",
        "fact_coverage",
        "rag_citation_resolution_rate",
        "rag_cited_fact_support_rate",
        "rag_expected_source_recall",
        "rag_abstention_accuracy",
        "avg_total_latency_ms",
        "p95_total_latency_ms",
    )
    diagnostic_metrics = (
        "regression_gate",
        "regression_failure_cases",
        "failure_reason_counts",
        "review_warning_counts",
        "execution_outcome_counts",
        "snapshot_unchanged",
        "metric_counts",
        "fact_assertion_counts",
        "execution_status_counts",
        "tool_status_counts",
        "fallback_reason_counts",
        "fallback_rate",
        "cases_with_fact_assertions",
        "partial_success_rate",
        "transport_error_rate",
        "http_error_rate",
        "evidence_available_rate",
        "avg_retry_count",
        "avg_latency_ms_router",
        "avg_latency_ms_planner",
        "avg_latency_ms_tool",
        "avg_observed_duration_ms_tool",
        "avg_completed_latency_ms_tool",
        "avg_latency_ms_answer",
    )
    lines = [
        "# Multi-Source Data Agent Core Evaluation Report",
        "",
        f"- Dataset: `{dataset_path}`",
        f"- Generated at: `{dt.datetime.now().isoformat(timespec='seconds')}`",
        "",
        "## Formal Metrics",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    literal_labels = {
        "fact_coverage": "Answer Literal Fact Coverage",
        "expected_fact_supported_by_evidence_rate": "Expected Fact Literal Occurrence in Evidence",
    }
    for key in formal_metrics:
        label = f"{literal_labels[key]} (`{key}`)" if key in literal_labels else f"`{key}`"
        lines.append(f"| {label} | {format_metric(summary.get(key))} |")
    lines.extend(["", "## Diagnostics", "", "| Metric | Value |", "| --- | ---: |"])
    for key in diagnostic_metrics:
        lines.append(f"| `{key}` | {md_escape(summary.get(key))} |")
    lines.extend(
        [
            "",
            "## Cases",
            "",
            "| Case | HTTP | Route | Route OK | Tools | Execution | Evidence | Evidence Literal Fact Check | Fallback | Latency ms | Error |",
            "| --- | ---: | --- | ---: | --- | --- | ---: | ---: | --- | ---: | --- |",
        ]
    )
    for result in results:
        metrics = result["metrics"]
        route = "{}/{}".format(result["actual"].get("route"), result["expected"].get("route"))
        lines.append(
            "| {case_id} | {http} | {route} | {route_ok} | {tools} | {execution} | {evidence} | {fact_support} | {fallback} | {latency} | {error} |".format(
                case_id=md_escape(result.get("id")),
                http=format_metric(result["actual"].get("http_status")),
                route=md_escape(route),
                route_ok=format_metric(metrics.get("route_correct")),
                tools=md_escape(", ".join(result["actual"].get("executed_tools") or [])),
                execution=md_escape(result["actual"].get("execution_status") or ""),
                evidence=format_metric(result["actual"].get("evidence_count")),
                fact_support=format_metric(metrics.get("expected_fact_supported_by_evidence")),
                fallback=md_escape(result.get("fallback_reason") or "none"),
                latency=format_metric(result.get("latency_ms")),
                error=md_escape(result.get("error") or ""),
            )
        )
    lines.extend(
        [
            "",
            "## Declared-check Failures and Review Warnings",
            "",
            "| Case | Outcome | Blocking checks | Review warnings |",
            "| --- | --- | --- | --- |",
        ]
    )
    for result in results:
        diagnostic = result.get("diagnostics", {})
        lines.append("| {} | {} | {} | {} |".format(
            md_escape(result.get("id")), md_escape(diagnostic.get("execution_outcome", "unobserved")),
            md_escape(", ".join(diagnostic.get("failure_reasons", [])) or "none"),
            md_escape(", ".join(diagnostic.get("warnings", [])) or "none"),
        ))
    lines.extend(
        [
            "",
            "## Metric Semantics",
            "",
            "- Answer Literal Fact Coverage (`fact_coverage`) measures expected-fact substring occurrence in the answer. The legacy JSON alias `answer_faithfulness` is identical; neither measures semantic faithfulness.",
            "- `expected_fact_supported_by_evidence` checks all expected-fact substrings in merged Evidence content. It does not verify source/row associations, actual citations or every answer claim; it is not semantic grounding.",
            "- Optional `asserted_fact_coverage` / `asserted_facts_ok_rate` check explicitly declared facts and row associations. They do not replace the historical literal metrics or prove general faithfulness; see per-case `fact_assertion_results`.",
            "- `answer_fact` is a finite phrase-occurrence check: negated or contradictory claims may still match. A passing assertion is not a semantic-correctness verdict; `review_checks` are prompts for review, not evidence that an independent human review occurred.",
            "- `fallback_reason` reports only real alternate-strategy fallback. Empty/error/transport outcomes are reported separately.",
            "- Expected policy rejection is separate from unexpected execution error. A timeout cannot pass a rejection check solely because both have status=error.",
            "- The optional regression gate checks declared expectations; literal mismatch warnings are not semantic error judgments. Review warnings before release, even when the automated gate passes.",
            "- Latency includes failures. Repeated measurements must retain every attempt and use the same source/dataset snapshot; do not select only successful runs.",
            "- `null` metrics are excluded from aggregate denominators rather than silently counted as success.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")

def thresholds_passed(summary: Dict[str, Any], args: argparse.Namespace) -> bool:
    ok = True
    if getattr(args, "fail_on_regression", False) and (
        summary.get("regression_failure_cases") != 0 or summary.get("snapshot_unchanged") is not True
        or summary.get("regression_gate", {}).get("passed") is False
    ):
        print("Regression gate failed: declared checks failed or the source/dataset changed during this run.", file=sys.stderr)
        ok = False
    if args.min_route_accuracy is not None:
        actual = summary.get("route_accuracy")
        if actual is None or actual < args.min_route_accuracy:
            print(
                f"Threshold failed: route_accuracy={actual} < {args.min_route_accuracy}",
                file=sys.stderr,
            )
            ok = False
    if args.max_p95_latency_ms is not None:
        actual = summary.get("p95_latency_ms")
        if actual is None or actual > args.max_p95_latency_ms:
            print(
                f"Threshold failed: p95_latency_ms={actual} > {args.max_p95_latency_ms}",
                file=sys.stderr,
            )
            ok = False
    return ok


def metric_status(value: Optional[Any]) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "pass" if value else "fail"
    return str(value)


def format_metric(value: Optional[Any]) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return f"{value:.4f}" if value <= 1 else f"{value:.2f}"
    return str(value)


def md_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
