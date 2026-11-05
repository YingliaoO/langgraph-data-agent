import type {
  ExecutionDTO,
  ExecutionEvidence,
  ExecutionStatement,
  ExecutionStatus,
  ExecutionTask,
  ExecutionToolResult,
  ToolResultStatus
} from "./types";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asNullableString(value: unknown): string | null | undefined {
  return typeof value === "string" ? value : value === null ? null : undefined;
}

function asFiniteNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asNullableNumber(value: unknown): number | null | undefined {
  if (value === null) return null;
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function asRecordArray(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function normalizeStatement(value: unknown): ExecutionStatement | null {
  if (!isRecord(value)) return null;
  const language = value.language;
  const text = value.text;
  if ((language !== "sql" && language !== "cypher") || typeof text !== "string") return null;
  return { language, text };
}

function normalizeTask(value: unknown): ExecutionTask | null {
  if (!isRecord(value)) return null;
  const taskId = asString(value.task_id);
  const question = asString(value.question);
  if (!taskId || !question) return null;
  return {
    task_id: taskId,
    question,
    tool_hint: asString(value.tool_hint, "auto"),
    depends_on: asStringArray(value.depends_on)
  };
}

function normalizeToolResult(value: unknown): ExecutionToolResult | null {
  if (!isRecord(value)) return null;
  const toolName = asString(value.tool_name);
  const status = value.status as ToolResultStatus;
  if (!toolName || !["success", "empty", "error"].includes(status)) return null;

  return {
    tool_name: toolName,
    tool_kind: asNullableString(value.tool_kind),
    status,
    task_id: asNullableString(value.task_id),
    task: asNullableString(value.task),
    statement: normalizeStatement(value.statement),
    result_count: Math.max(0, asFiniteNumber(value.result_count)),
    rows_preview: asRecordArray(value.rows_preview),
    records_preview: asRecordArray(value.records_preview),
    documents_preview: asRecordArray(value.documents_preview),
    evidence_ids: asStringArray(value.evidence_ids),
    error_code: asNullableString(value.error_code),
    error_category: asNullableString(value.error_category),
    repair_count: Math.max(0, asFiniteNumber(value.repair_count))
  };
}

function normalizeEvidence(value: unknown): ExecutionEvidence | null {
  if (!isRecord(value)) return null;
  const evidenceId = asString(value.evidence_id);
  const kind = asString(value.kind);
  if (!evidenceId || !kind) return null;

  return {
    evidence_id: evidenceId,
    kind,
    source: asNullableString(value.source),
    source_id: asNullableString(value.source_id),
    content: value.content,
    score: asNullableNumber(value.score),
    rank: asNullableNumber(value.rank),
    task_id: asNullableString(value.task_id),
    metadata: isRecord(value.metadata) ? value.metadata : {}
  };
}

function normalizeTimings(value: unknown): Record<string, number> {
  if (!isRecord(value)) return {};
  return Object.entries(value).reduce<Record<string, number>>((result, [key, raw]) => {
    if (typeof raw === "number" && Number.isFinite(raw)) result[key] = raw;
    return result;
  }, {});
}

export function normalizeExecution(value: unknown): ExecutionDTO | undefined {
  if (!isRecord(value) || value.contract_version !== "1") return undefined;

  const status = value.status as ExecutionStatus;
  if (!["success", "empty", "partial", "error"].includes(status)) return undefined;

  return {
    contract_version: "1",
    status,
    tasks: Array.isArray(value.tasks)
      ? value.tasks.map(normalizeTask).filter((item): item is ExecutionTask => item !== null)
      : [],
    tool_results: Array.isArray(value.tool_results)
      ? value.tool_results
          .map(normalizeToolResult)
          .filter((item): item is ExecutionToolResult => item !== null)
      : [],
    evidence: Array.isArray(value.evidence)
      ? value.evidence
          .map(normalizeEvidence)
          .filter((item): item is ExecutionEvidence => item !== null)
      : [],
    citation_ids: asStringArray(value.citation_ids),
    timings: normalizeTimings(value.timings),
    fallback_reason: asString(value.fallback_reason, "none")
  };
}
