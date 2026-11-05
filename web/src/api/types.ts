export type ExecutionStatus = "success" | "empty" | "partial" | "error";
export type ToolResultStatus = "success" | "empty" | "error";

export interface ExecutionStatement {
  language: "sql" | "cypher";
  text: string;
}

export interface ExecutionTask {
  task_id: string;
  question: string;
  tool_hint: string;
  depends_on: string[];
}

export interface ExecutionEvidence {
  evidence_id: string;
  kind: string;
  source?: string | null;
  source_id?: string | null;
  content: unknown;
  score?: number | null;
  rank?: number | null;
  task_id?: string | null;
  metadata: Record<string, unknown>;
}

export interface ExecutionToolResult {
  tool_name: string;
  tool_kind?: string | null;
  status: ToolResultStatus;
  task_id?: string | null;
  task?: string | null;
  statement?: ExecutionStatement | null;
  result_count: number;
  rows_preview: Array<Record<string, unknown>>;
  records_preview: Array<Record<string, unknown>>;
  documents_preview: Array<Record<string, unknown>>;
  evidence_ids: string[];
  error_code?: string | null;
  error_category?: string | null;
  repair_count: number;
}

export interface ExecutionDTO {
  contract_version: "1";
  status: ExecutionStatus;
  tasks: ExecutionTask[];
  tool_results: ExecutionToolResult[];
  evidence: ExecutionEvidence[];
  citation_ids: string[];
  timings: Record<string, number>;
  fallback_reason: string;
}

export interface ChatResponse {
  message?: string;
  session_id?: string;
  route?: string | null;
  route_logic?: string | null;
  sources?: unknown[];
  execution?: unknown;
  metadata?: Record<string, unknown>;
}
