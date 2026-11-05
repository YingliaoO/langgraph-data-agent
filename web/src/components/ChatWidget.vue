<template>
  <div class="chat-entry">
    <transition name="fade">
      <button
        v-if="!isOpen"
        class="floating-button"
        type="button"
        @click="openChat"
      >
        <span class="icon">⌘</span>
        <span class="label">Open Data Agent</span>
      </button>
    </transition>

    <transition name="pop">
      <div v-if="isOpen" class="chat-panel">
        <header class="chat-header">
          <div class="header-content">
            <div class="title-row">
              <div class="title">Data Agent Console</div>
              <div class="session-chip">
                {{ sessionId ? "会话ID：" + sessionId : "新会话" }}
              </div>
            </div>
            <div class="subtitle">
              <span class="status-dot"></span>
              LangGraph · Enterprise Data Agent
            </div>
          </div>
          <button class="close-btn" type="button" @click="closeChat">×</button>
        </header>

        <div class="console-content">
          <label class="auth-input">API Bearer Token（仅保存在当前页面内存）
            <input v-model="accessToken" type="password" autocomplete="off" placeholder="粘贴管理员签发的短期访问令牌" aria-label="API Bearer Token" />
          </label>
          <section ref="messageContainer" class="chat-body">
          <div class="suggestion-bar">
            <button
              v-for="(item, idx) in quickSuggestions"
              :key="idx"
              type="button"
              class="suggestion-chip"
              @click="sendSuggestion(item.prompt)"
            >
              <span class="chip-icon">⚡</span>
              <span>{{ item.label }}</span>
            </button>
          </div>
          <div v-if="messages.length === 0" class="empty-hint">
            <p>输入一个数据问题，查看 Agent 的回答与只读执行 Trace。</p>
            <ul>
              <li>当前演示域为 synthetic Enterprise Retail，可尝试订单统计、政策检索与商品关系问题。</li>
              <li>也可以上传 CSV/Excel 数据集并提交摄取任务。</li>
            </ul>
          </div>

          <div
            v-for="(message, index) in messages"
            :key="index"
            :class="['message', message.role]"
          >
            <div class="bubble">
              <p class="content" v-html="linkify(message.content)"></p>
              <div
                v-if="message.role === 'assistant' && message.uxState"
                :class="['execution-notice', `is-${message.uxState}`]"
              >
                <strong>{{ uxStateTitle(message.uxState) }}</strong>
                <span>{{ uxStateDescription(message.uxState) }}</span>
              </div>
              <div v-if="message.sources?.length" class="sources">
                <span class="sources-title">参考来源</span>
                <ul>
                  <li v-for="(source, sIdx) in message.sources" :key="sIdx">
                    {{ formatSource(source) }}
                  </li>
                </ul>
              </div>
              <div v-if="message.route" class="route">
                路由：{{ message.route }}
                <span v-if="message.routeLogic">（{{ message.routeLogic }}）</span>
              </div>
            </div>
          </div>

            <div v-if="isTyping" class="message assistant">
              <div class="bubble typing">
                <span class="typing-label">Running agent...</span>
                <span class="dot"></span>
                <span class="dot"></span>
                <span class="dot"></span>
              </div>
            </div>
          </section>

          <aside class="trace-panel" aria-label="Agent trace">
            <div class="trace-header">
              <div>
                <span class="eyebrow">OBSERVABILITY</span>
                <h2>Trace Panel</h2>
              </div>
              <span v-if="activeExecution || activeTrace" class="trace-state">
                {{ activeExecution ? "EXECUTION" : "TRACE" }}
              </span>
            </div>

            <div v-if="activeExecution" class="trace-content execution-content">
              <dl class="trace-fields">
                <div>
                  <dt>Status</dt>
                  <dd :class="['execution-status', `is-${activeExecution.status}`]">
                    {{ activeExecution.status }}
                  </dd>
                </div>
                <div>
                  <dt>Contract</dt>
                  <dd>v{{ activeExecution.contract_version }}</dd>
                </div>
                <div>
                  <dt>Fallback</dt>
                  <dd>{{ activeExecution.fallback_reason || "none" }}</dd>
                </div>
              </dl>

              <section v-if="activeExecution.tasks.length" class="trace-section">
                <h3>Plan</h3>
                <ol class="execution-list">
                  <li v-for="task in activeExecution.tasks" :key="task.task_id">
                    <strong>{{ task.task_id }}</strong>
                    <span>{{ task.question }}</span>
                    <small>tool={{ task.tool_hint }}</small>
                  </li>
                </ol>
              </section>

              <section v-if="activeExecution.tool_results.length" class="trace-section">
                <h3>Tool Results</h3>
                <div class="tool-result-list">
                  <article
                    v-for="result in activeExecution.tool_results"
                    :key="`${result.task_id || result.tool_name}:${result.tool_name}`"
                    class="tool-result-card"
                  >
                    <div class="tool-result-head">
                      <strong>{{ result.tool_name }}</strong>
                      <span :class="['tool-status', `is-${result.status}`]">{{ result.status }}</span>
                    </div>
                    <div class="tool-meta">
                      <span v-if="result.tool_kind">{{ result.tool_kind }}</span>
                      <span>count={{ result.result_count }}</span>
                      <span v-if="result.repair_count">repair={{ result.repair_count }}</span>
                    </div>
                    <p v-if="result.task" class="tool-task">{{ result.task }}</p>
                    <pre v-if="result.statement" class="sql-block">{{ result.statement.text }}</pre>
                    <p v-if="result.error_code || result.error_category" class="tool-error">
                      {{ result.error_code || result.error_category }}
                    </p>
                  </article>
                </div>
              </section>

              <section v-if="activeExecution.evidence.length" class="trace-section">
                <h3>Evidence</h3>
                <ul class="evidence-list">
                  <li v-for="item in activeExecution.evidence" :key="item.evidence_id">
                    <div class="evidence-head">
                      <strong>{{ item.evidence_id }}</strong>
                      <span>{{ item.source || item.kind }}</span>
                    </div>
                    <p>{{ formatExecutionValue(item.content) }}</p>
                    <small v-if="item.source_id">{{ item.source_id }}</small>
                  </li>
                </ul>
              </section>

              <section v-if="Object.keys(activeExecution.timings).length" class="trace-section">
                <h3>Timing</h3>
                <dl class="timing-grid">
                  <div v-for="(value, key) in activeExecution.timings" :key="key">
                    <dt>{{ key }}</dt>
                    <dd>{{ formatTiming(value) }}</dd>
                  </div>
                </dl>
              </section>

              <section v-if="activeExecution.citation_ids.length" class="trace-section">
                <h3>Citations</h3>
                <div class="citation-list">
                  <code v-for="citation in activeExecution.citation_ids" :key="citation">{{ citation }}</code>
                </div>
              </section>
            </div>

            <div v-else-if="activeTrace" class="trace-content">
              <dl class="trace-fields">
                <div v-if="activeTrace.route">
                  <dt>Route</dt>
                  <dd>{{ activeTrace.route }}</dd>
                </div>
                <div v-if="activeTrace.routeLogic">
                  <dt>Route Logic</dt>
                  <dd>{{ activeTrace.routeLogic }}</dd>
                </div>
                <div v-if="activeTrace.traceId">
                  <dt>Trace ID</dt>
                  <dd class="trace-id">{{ activeTrace.traceId }}</dd>
                </div>
                <div v-if="activeTrace.durationMs !== undefined">
                  <dt>Duration</dt>
                  <dd>{{ activeTrace.durationMs }} ms</dd>
                </div>
                <div v-if="activeTrace.fallback !== undefined">
                  <dt>Fallback</dt>
                  <dd>{{ activeTrace.fallback ? "Yes" : "No" }}</dd>
                </div>
              </dl>

              <section v-if="activeTrace.selectedTools?.length" class="trace-section">
                <h3>Selected Tools</h3>
                <ul class="trace-list">
                  <li v-for="(tool, toolIndex) in activeTrace.selectedTools" :key="toolIndex">
                    {{ formatTraceValue(tool) }}
                  </li>
                </ul>
              </section>

              <section v-if="activeTrace.sql" class="trace-section">
                <h3>SQL</h3>
                <pre class="sql-block">{{ activeTrace.sql }}</pre>
              </section>

              <section v-if="activeTrace.events?.length" class="trace-section">
                <h3>Events</h3>
                <ol class="event-list">
                  <li v-for="(event, eventIndex) in activeTrace.events" :key="eventIndex">
                    <span>{{ formatEvent(event) }}</span>
                  </li>
                </ol>
              </section>

              <section v-if="activeTrace.sources?.length" class="trace-section">
                <h3>Sources</h3>
                <ul class="trace-list">
                  <li v-for="(source, sourceIndex) in activeTrace.sources" :key="sourceIndex">
                    {{ formatSource(source) }}
                  </li>
                </ul>
              </section>
            </div>
            <p v-else class="trace-empty">Trace data will appear after the next response.</p>
          </aside>
        </div>

        <footer class="chat-footer">
          <div class="upload-row">
            <label class="upload-button">
              <input type="file" class="hidden" @change="onFileSelected" />
              <span>📎 上传文件</span>
            </label>
            <div v-if="uploadStatus" class="upload-status">
              {{ uploadStatus }}
            </div>
            <button
              v-if="sessionId"
              type="button"
              class="reset-session"
              @click="resetSession"
            >
              开启新会话
            </button>
          </div>
          <form class="input-row" @submit.prevent="sendMessage">
            <textarea
              v-model="userInput"
              :placeholder="placeholder"
              :disabled="isTyping"
              rows="2"
              @keydown.enter.exact.prevent="sendMessage"
            />
            <button class="send-button" type="submit" :disabled="isTyping || !userInput.trim()">
              发送
            </button>
          </form>
        </footer>
      </div>
    </transition>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import axios from "axios";
import { normalizeExecution } from "../api/execution";
import type { ChatResponse, ExecutionDTO } from "../api/types";

type MessageUxState =
  | "success"
  | "empty"
  | "partial"
  | "error"
  | "policy_rejection"
  | "timeout"
  | "transport_error";

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  route?: string | null;
  routeLogic?: string | null;
  sources?: Array<Record<string, unknown>>;
  execution?: ExecutionDTO;
  uxState?: MessageUxState;
  trace?: TraceSnapshot;
}

interface TraceEvent {
  [key: string]: unknown;
}

interface TraceSnapshot {
  route?: string;
  routeLogic?: string;
  traceId?: string;
  selectedTools?: unknown[];
  events?: TraceEvent[];
  durationMs?: number | string;
  fallback?: boolean;
  sources?: Array<Record<string, unknown>>;
  sql?: string;
}

const API_BASE = import.meta.env.VITE_API_BASE_URL || "";
const accessToken = ref("");

function authHeaders(): Record<string, string> {
  if (!accessToken.value.trim()) throw new Error("请先填写有效的 API Bearer Token");
  return { Authorization: `Bearer ${accessToken.value.trim()}` };
}

const defaultState = () => ({
  isOpen: false,
  isTyping: false,
  sessionId: "",
  userInput: "",
  messages: [] as ChatMessage[],
  uploadStatus: "",
  attachedFilePath: "",
  attachedFileName: ""
});

const state = reactive(defaultState());

const messageContainer = ref<HTMLElement | null>(null);

const isOpen = computed(() => state.isOpen);
const isTyping = computed(() => state.isTyping);
const sessionId = computed(() => state.sessionId);
const userInput = computed({
  get: () => state.userInput,
  set: (val: string) => {
    state.userInput = val;
  }
});
const messages = computed(() => state.messages);
const uploadStatus = computed(() => state.uploadStatus);
const activeExecution = computed<ExecutionDTO | null>(() => {
  for (let index = state.messages.length - 1; index >= 0; index -= 1) {
    const message = state.messages[index];
    if (message.role === "assistant" && message.execution) {
      return message.execution;
    }
  }
  return null;
});
const activeTrace = computed<TraceSnapshot | null>(() => {
  for (let index = state.messages.length - 1; index >= 0; index -= 1) {
    const message = state.messages[index];
    if (message.role === "assistant" && message.trace) {
      return message.trace;
    }
  }
  return null;
});

const placeholder = computed(() => {
  if (state.isTyping) return "正在生成回复...";
  if (state.attachedFileName) return `文件已上传：${state.attachedFileName}，请输入问题`;
  return "输入数据问题，例如“请使用 MySQL 查询 retail_orders 当前一共有多少个订单”";
});

const quickSuggestions = [
  {
    label: "Text2SQL 统计",
    prompt: "使用 MySQL 统计各商品品类销售件数，并按销量从高到低排序。"
  },
  {
    label: "Knowledge Retrieval",
    prompt: "电子产品无质量问题的情况下，多少天内可以申请退货？"
  },
  {
    label: "Graph Reasoning",
    prompt: "Aurora ANC Headphones 由哪个供应商供货？"
  },
  {
    label: "Agent Trace",
    prompt: "请回答当前演示数据域包含哪些能力，并让我查看本次执行路径。"
  }
] as const;

function openChat() {
  state.isOpen = true;
}

function closeChat() {
  state.isOpen = false;
}

defineExpose({
  openChat,
  closeChat
});
function resetSession() {
  state.sessionId = "";
  state.messages = [];
  state.uploadStatus = "";
  state.attachedFileName = "";
  state.attachedFilePath = "";
}

watch(accessToken, () => resetSession());

async function onFileSelected(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;

  state.uploadStatus = "正在上传...";
  try {
    const formData = new FormData();
    formData.append("file", file);
    const { data } = await axios.post(`${API_BASE}/api/v1/upload/file`, formData, {
      headers: authHeaders()
    });
    if (data?.success) {
      state.attachedFilePath = data.file_id;
      state.attachedFileName = data.original_name || file.name;
      state.uploadStatus = `已上传：${state.attachedFileName}`;
    } else {
      throw new Error(data?.detail || "上传失败");
    }
  } catch (error: unknown) {
    console.error("File upload failed", error);
    state.uploadStatus = "上传失败，请重试";
  } finally {
    // reset input to allow re-upload same file
    input.value = "";
  }
}

function sendSuggestion(prompt: string) {
  if (state.isTyping) return;
  state.userInput = prompt;
  void sendMessage();
}

async function sendMessage() {
  const message = state.userInput.trim();
  if (!message || state.isTyping) {
    return;
  }

  const userMessage: ChatMessage = { role: "user", content: message };
  state.messages.push(userMessage);
  state.userInput = "";
  state.isTyping = true;

  try {
    const payload: Record<string, unknown> = {
      message,
      session_id: state.sessionId || undefined,
      stream: false
    };

    if (state.attachedFilePath) {
      payload.file_id = state.attachedFilePath;
      payload.ingest_incremental = true;
    }

    const { data } = await axios.post<ChatResponse>(`${API_BASE}/api/v1/chat/`, payload, { headers: authHeaders() });
    const execution = normalizeExecution(data.execution);

    if (data?.message) {
      const assistantMessage: ChatMessage = {
        role: "assistant",
        content: data.message,
        route: data.route || null,
        routeLogic: data.route_logic || null,
        sources: normalizeSources(data.sources),
        execution,
        uxState: classifyExecutionUxState(execution),
        trace: normalizeTrace(data as Record<string, unknown>)
      };
      state.messages.push(assistantMessage);
    } else {
      state.messages.push({
        role: "assistant",
        execution,
        uxState: classifyExecutionUxState(execution) || "error",
        content: "抱歉，未能获取到有效的响应。"
      });
    }

    if (data?.session_id) {
      state.sessionId = data.session_id;
    }

    // 一次性消费文件
    state.attachedFilePath = "";
    state.attachedFileName = "";
    state.uploadStatus = "";
  } catch (error: unknown) {
    console.error("Chat request failed", error);
    state.messages.push({
      role: "assistant",
      uxState: "transport_error",
      content: "请求出错了，请稍后重试。"
    });
  } finally {
    state.isTyping = false;
  }
}

function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (character) => {
    switch (character) {
      case "&": return "&amp;";
      case "<": return "&lt;";
      case ">": return "&gt;";
      case '"': return "&quot;";
      case "'": return "&#39;";
      default: return character;
    }
  });
}

function linkify(text: string): string {
  if (!text) return "";
  const parts: string[] = [];
  let offset = 0;
  for (const match of text.matchAll(/https?:\/\/[^\s<>"']+/g)) {
    const index = match.index ?? 0;
    parts.push(escapeHtml(text.slice(offset, index)));
    const url = escapeHtml(match[0]);
    parts.push(`<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`);
    offset = index + match[0].length;
  }
  parts.push(escapeHtml(text.slice(offset)));
  return parts.join("");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function normalizeSources(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.reduce<Array<Record<string, unknown>>>((result, source) => {
    if (typeof source === "string") {
      result.push({ document_id: source, source });
    } else if (isRecord(source)) {
      result.push(source);
    }
    return result;
  }, []);
}

function normalizeTrace(data: Record<string, unknown>): TraceSnapshot | undefined {
  const metadata = isRecord(data.metadata) ? data.metadata : undefined;
  const evalTrace = metadata && isRecord(metadata.eval_trace) ? metadata.eval_trace : undefined;
  const events = evalTrace && Array.isArray(evalTrace.events)
    ? evalTrace.events.filter(isRecord)
    : undefined;
  const selectedTools = evalTrace && Array.isArray(evalTrace.selected_tools)
    ? evalTrace.selected_tools
    : undefined;
  const sources = normalizeSources(data.sources);
  const trace: TraceSnapshot = {
    route: typeof data.route === "string"
      ? data.route
      : (evalTrace && typeof evalTrace.route === "string" ? evalTrace.route : undefined),
    routeLogic: typeof data.route_logic === "string" ? data.route_logic : undefined,
    traceId: metadata && typeof metadata.trace_id === "string" ? metadata.trace_id : undefined,
    selectedTools,
    events,
    durationMs: evalTrace && (typeof evalTrace.duration_ms === "number" || typeof evalTrace.duration_ms === "string")
      ? evalTrace.duration_ms
      : undefined,
    fallback: evalTrace && typeof evalTrace.fallback === "boolean" ? evalTrace.fallback : undefined,
    sources,
    sql: extractSql(events)
  };

  return Object.values(trace).some((value) => value !== undefined && (!Array.isArray(value) || value.length > 0))
    ? trace
    : undefined;
}

function extractSql(events: TraceEvent[] | undefined): string | undefined {
  if (!events) return undefined;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.stage !== "tool_execution" || event.action !== "done") continue;
    const tool = typeof event.tool === "string" ? event.tool.toLowerCase() : "";
    if (!tool.includes("text2sql")) continue;

    const eventMetadata = isRecord(event.metadata) ? event.metadata : undefined;
    const metadataSql = eventMetadata?.sql;
    if (typeof metadataSql === "string" && metadataSql.trim()) return metadataSql;
    if (typeof event.sql === "string" && event.sql.trim()) return event.sql;
  }
  return undefined;
}

function formatTraceValue(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function classifyExecutionUxState(execution: ExecutionDTO | undefined): MessageUxState | undefined {
  if (!execution) return undefined;
  if (execution.status !== "error") return execution.status;

  const markers = [
    execution.fallback_reason,
    ...execution.tool_results.flatMap((result) => [result.error_code, result.error_category])
  ]
    .filter((value): value is string => typeof value === "string" && value.length > 0)
    .join(" ")
    .toLowerCase();

  if (
    markers.includes("read_only") ||
    markers.includes("read-only") ||
    markers.includes("policy_rejection") ||
    markers.includes("policy violation") ||
    markers.includes("policy")
  ) {
    return "policy_rejection";
  }

  if (
    markers.includes("timeout") ||
    markers.includes("timed out") ||
    markers.includes("timed_out")
  ) {
    return "timeout";
  }

  return "error";
}

function uxStateTitle(state: MessageUxState): string {
  return {
    success: "Completed",
    empty: "No matching data",
    partial: "Partial result",
    error: "Execution failed",
    policy_rejection: "Request blocked by policy",
    timeout: "Execution timed out",
    transport_error: "Connection failed"
  }[state];
}

function uxStateDescription(state: MessageUxState): string {
  return {
    success: "The Agent completed this request successfully.",
    empty: "The request completed normally, but no matching data or evidence was found.",
    partial: "Some tasks succeeded and some failed. Available results are still shown.",
    error: "The Agent returned an execution error. Inspect Tool Results for details.",
    policy_rejection: "The requested operation was blocked by the read-only or safety policy.",
    timeout: "Execution exceeded its configured time budget.",
    transport_error: "No usable Agent response was received. Check the API or network and retry."
  }[state];
}

function formatExecutionValue(value: unknown): string {
  const rendered = formatTraceValue(value);
  return rendered.length > 260 ? `${rendered.slice(0, 257)}...` : rendered;
}

function formatTiming(value: number): string {
  return `${Math.round(value * 100) / 100} ms`;
}

function formatEvent(event: TraceEvent): string {
  const stage = typeof event.stage === "string" ? event.stage : "event";
  const action = typeof event.action === "string" ? event.action : "";
  const tool = typeof event.tool === "string" ? ` · ${event.tool}` : "";
  return `${stage}${action ? ` · ${action}` : ""}${tool}`;
}

function formatSource(source: Record<string, unknown>): string {
  const docId = source.document_id || source.source || source.id || source.name;
  const name = source.name || source.title || "";
  if (name && docId && name !== docId) {
    return `${name} (${docId})`;
  }
  return String(docId || JSON.stringify(source));
}

function handleKeydown(event: KeyboardEvent) {
  if (event.key === "Escape" && state.isOpen) {
    closeChat();
  }
}

watch(
  () => state.messages.length,
  async () => {
    await nextTick();
    if (messageContainer.value) {
      messageContainer.value.scrollTop = messageContainer.value.scrollHeight;
    }
  }
);

onMounted(() => {
  window.addEventListener("keydown", handleKeydown);
});

onUnmounted(() => {
  window.removeEventListener("keydown", handleKeydown);
});
</script>

<style scoped lang="scss">
.chat-entry {
  position: fixed;
  bottom: 24px;
  right: 24px;
  z-index: 1000;
  font-size: 14px;
  color: #1f2937;
}

.floating-button {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  border-radius: 999px;
  border: none;
  background: linear-gradient(135deg, #f97316, #ef4444);
  color: #fff;
  font-weight: 600;
  cursor: pointer;
  box-shadow: 0 15px 30px rgba(249, 115, 22, 0.35);
  transition: transform 0.2s ease, box-shadow 0.2s ease;

  &:hover {
    transform: translateY(-2px);
    box-shadow: 0 20px 34px rgba(239, 68, 68, 0.4);
  }

  .icon {
    font-size: 18px;
  }
}

.chat-panel {
  width: min(980px, calc(100vw - 32px));
  height: clamp(460px, 50vh, 640px);
  display: flex;
  flex-direction: column;
  border-radius: 20px;
  background: #fffdfb;
  box-shadow: 0 28px 60px rgba(15, 23, 42, 0.18);
  overflow: hidden;
  border: 1px solid rgba(248, 187, 125, 0.35);
}

.console-content {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 270px;
}

.chat-header {
  padding: 18px 20px;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  background: linear-gradient(135deg, #f97316, #ef4444);
  color: #fff;
  box-shadow: inset 0 -1px 0 rgba(255, 255, 255, 0.2);

  .header-content {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .title-row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .title {
    font-size: 16px;
    font-weight: 600;
    letter-spacing: 0.5px;
  }

  .subtitle {
    font-size: 12px;
    opacity: 0.85;
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .session-chip {
    padding: 2px 8px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.16);
    font-size: 11px;
    font-weight: 500;
    backdrop-filter: blur(4px);
    max-width: 220px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .status-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: #fef3c7;
    box-shadow: 0 0 0 4px rgba(254, 243, 199, 0.25);
  }

  .close-btn {
    border: none;
    background: transparent;
    color: inherit;
    font-size: 24px;
    cursor: pointer;
    line-height: 1;
    transition: transform 0.2s ease, opacity 0.2s ease;

    &:hover {
      opacity: 0.8;
      transform: scale(1.05);
    }
  }
}

.chat-body {
  min-width: 0;
  min-height: 0;
  padding: 16px;
  overflow-y: auto;
  background: linear-gradient(180deg, rgba(255, 244, 230, 0.6), rgba(255, 251, 247, 0.95));
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.trace-panel {
  min-width: 0;
  overflow-y: auto;
  padding: 16px;
  background: #172033;
  color: #e2e8f0;
  border-left: 1px solid rgba(148, 163, 184, 0.22);
}

.trace-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 16px;

  h2 {
    margin: 3px 0 0;
    font-size: 16px;
    font-weight: 650;
    color: #f8fafc;
  }
}

.eyebrow {
  color: #94a3b8;
  font-size: 9px;
  letter-spacing: 0.13em;
  font-weight: 700;
}

.trace-state {
  padding: 3px 6px;
  border-radius: 999px;
  background: rgba(45, 212, 191, 0.14);
  color: #5eead4;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.trace-fields {
  display: grid;
  gap: 10px;
  margin: 0;

  div {
    display: grid;
    gap: 3px;
  }

  dt {
    color: #94a3b8;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }

  dd {
    margin: 0;
    color: #f8fafc;
    font-size: 12px;
    overflow-wrap: anywhere;
  }

  .trace-id {
    color: #a5b4fc;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 10px;
  }
}

.trace-section {
  margin-top: 18px;

  h3 {
    margin: 0 0 8px;
    color: #cbd5e1;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }
}

.trace-list,
.event-list {
  margin: 0;
  padding-left: 16px;
  color: #cbd5e1;
  font-size: 11px;
  line-height: 1.45;

  li + li {
    margin-top: 5px;
  }
}

.event-list {
  color: #94a3b8;
}

.sql-block {
  max-height: 150px;
  margin: 0;
  overflow: auto;
  padding: 9px;
  border-radius: 7px;
  background: #0f172a;
  color: #a7f3d0;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 10px;
  line-height: 1.45;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.execution-content {
  .execution-status,
  .tool-status {
    display: inline-flex;
    width: fit-content;
    padding: 2px 6px;
    border-radius: 999px;
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    background: rgba(148, 163, 184, 0.16);
    color: #e2e8f0;

    &.is-success { color: #86efac; background: rgba(34, 197, 94, 0.14); }
    &.is-empty { color: #fde68a; background: rgba(245, 158, 11, 0.14); }
    &.is-partial { color: #93c5fd; background: rgba(59, 130, 246, 0.14); }
    &.is-error { color: #fca5a5; background: rgba(239, 68, 68, 0.14); }
  }
}

.execution-list,
.evidence-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: grid;
  gap: 8px;

  li {
    display: grid;
    gap: 3px;
    padding: 8px;
    border-radius: 8px;
    background: rgba(15, 23, 42, 0.6);
    border: 1px solid rgba(148, 163, 184, 0.15);
    font-size: 10px;
  }

  small {
    color: #94a3b8;
    overflow-wrap: anywhere;
  }
}

.tool-result-list {
  display: grid;
  gap: 8px;
}

.tool-result-card {
  padding: 9px;
  border-radius: 9px;
  background: rgba(15, 23, 42, 0.72);
  border: 1px solid rgba(148, 163, 184, 0.16);
}

.tool-result-head,
.evidence-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  color: #f8fafc;
  font-size: 10px;
}

.tool-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 5px;
  color: #94a3b8;
  font-size: 9px;
}

.tool-task,
.tool-error,
.evidence-list p {
  margin: 6px 0 0;
  color: #cbd5e1;
  font-size: 10px;
  overflow-wrap: anywhere;
}

.tool-error { color: #fca5a5; }

.timing-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
  margin: 0;

  div {
    padding: 6px;
    border-radius: 7px;
    background: rgba(15, 23, 42, 0.58);
  }

  dt { color: #94a3b8; font-size: 9px; }
  dd { margin: 2px 0 0; color: #f8fafc; font-size: 10px; }
}

.citation-list {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;

  code {
    padding: 3px 5px;
    border-radius: 5px;
    background: rgba(99, 102, 241, 0.14);
    color: #c7d2fe;
    font-size: 9px;
  }
}

.trace-empty {
  margin: 0;
  color: #94a3b8;
  font-size: 12px;
  line-height: 1.5;
}

.suggestion-bar {
  position: sticky;
  top: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding-bottom: 12px;
  margin-bottom: 4px;
  background: linear-gradient(180deg, rgba(255, 251, 247, 0.92), rgba(255, 251, 247, 0));
  z-index: 1;
}

.suggestion-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border-radius: 999px;
  border: 1px solid rgba(249, 115, 22, 0.45);
  background: rgba(255, 247, 237, 0.85);
  color: #9a3412;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s ease;

  &:hover {
    background: linear-gradient(135deg, rgba(249, 115, 22, 0.15), rgba(239, 68, 68, 0.15));
    transform: translateY(-1px);
    box-shadow: 0 4px 10px rgba(239, 68, 68, 0.2);
  }

  .chip-icon {
    font-size: 14px;
  }
}

.empty-hint {
  padding: 18px;
  border-radius: 12px;
  background: rgba(254, 215, 170, 0.5);
  border: 1px dashed rgba(249, 115, 22, 0.4);
  color: #7c2d12;
  font-size: 13px;

  ul {
    padding-left: 18px;
    margin: 10px 0 0;
  }
}

.message {
  display: flex;
  align-items: flex-start;

  &.user {
    justify-content: flex-end;

    .bubble {
      background: linear-gradient(135deg, #f97316, #f97316 40%, #ef4444);
      color: #fff;
      border-bottom-right-radius: 4px;
      box-shadow: 0 12px 24px rgba(239, 68, 68, 0.25);
    }
  }

  &.assistant {
    .bubble {
      background: rgba(255, 255, 255, 0.92);
      color: #1f2937;
      border-bottom-left-radius: 4px;
      border: 1px solid rgba(248, 187, 125, 0.4);
      backdrop-filter: blur(4px);
    }
  }
}

.bubble {
  max-width: 80%;
  padding: 12px 14px;
  border-radius: 14px;
  line-height: 1.5;
  box-shadow: 0 6px 16px rgba(15, 23, 42, 0.08);
  font-size: 13px;

  a {
    color: #f97316;
    text-decoration: none;

    &:hover {
      text-decoration: underline;
    }
  }
}

.execution-notice {
  display: grid;
  gap: 2px;
  margin-top: 9px;
  padding: 8px 10px;
  border-radius: 9px;
  border: 1px solid rgba(148, 163, 184, 0.28);
  background: rgba(241, 245, 249, 0.75);
  color: #334155;
  font-size: 11px;

  strong { font-size: 11px; }
  span { color: #64748b; line-height: 1.4; }

  &.is-success {
    border-color: rgba(34, 197, 94, 0.28);
    background: rgba(240, 253, 244, 0.88);
    color: #166534;
  }

  &.is-empty {
    border-color: rgba(245, 158, 11, 0.32);
    background: rgba(255, 251, 235, 0.9);
    color: #92400e;
  }

  &.is-partial {
    border-color: rgba(59, 130, 246, 0.3);
    background: rgba(239, 246, 255, 0.9);
    color: #1d4ed8;
  }

  &.is-error,
  &.is-policy_rejection,
  &.is-timeout,
  &.is-transport_error {
    border-color: rgba(239, 68, 68, 0.3);
    background: rgba(254, 242, 242, 0.9);
    color: #991b1b;
  }
}

.sources {
  margin-top: 10px;
  padding: 12px;
  border-radius: 12px;
  background: rgba(254, 215, 170, 0.35);
  border: 1px dashed rgba(249, 115, 22, 0.45);

  .sources-title {
    font-size: 12px;
    font-weight: 600;
    color: #9a3412;
  }

  ul {
    margin: 6px 0 0;
    padding-left: 18px;
    font-size: 12px;
    color: #1f2937;
  }
}

.route {
  margin-top: 8px;
  font-size: 11px;
  color: #b45309;
}

.typing {
  display: inline-flex;
  align-items: center;
  gap: 4px;

  .typing-label {
    margin-right: 4px;
    color: #64748b;
    font-size: 11px;
  }

  .dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #f97316;
    animation: pulse 1.2s infinite ease-in-out;

    &:nth-child(2) {
      animation-delay: 0.2s;
    }
    &:nth-child(3) {
      animation-delay: 0.4s;
    }
  }
}

@keyframes pulse {
  0%, 80%, 100% {
    opacity: 0.4;
    transform: translateY(0);
  }
  40% {
    opacity: 1;
    transform: translateY(-3px);
  }
}

.chat-footer {
  padding: 12px 16px 16px;
  background: #fff;
  border-top: 1px solid rgba(248, 187, 125, 0.35);
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.upload-row {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 12px;

  .upload-button {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 10px;
    border-radius: 8px;
    background: rgba(254, 215, 170, 0.4);
    color: #c2410c;
    font-weight: 600;
    cursor: pointer;

    .hidden {
      display: none;
    }
  }

  .upload-status {
    color: #92400e;
  }

  .reset-session {
    margin-left: auto;
    border: none;
    background: transparent;
    color: #fb923c;
    cursor: pointer;
    font-size: 12px;

    &:hover {
      color: #ea580c;
    }
  }
}

.input-row {
  display: flex;
  gap: 10px;
  align-items: flex-end;

  textarea {
    flex: 1;
    resize: none;
    border: 1px solid rgba(148, 163, 184, 0.4);
    border-radius: 12px;
    padding: 10px 12px;
    font-family: inherit;
    font-size: 13px;
    outline: none;

    &:focus {
      border-color: #f97316;
      box-shadow: 0 0 0 3px rgba(249, 115, 22, 0.18);
    }
  }

  .send-button {
    border: none;
    border-radius: 12px;
    padding: 10px 16px;
    background: linear-gradient(135deg, #f97316, #ef4444);
    color: #fff;
    font-weight: 600;
    cursor: pointer;
    transition: opacity 0.2s ease;

    &:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
  }
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

.pop-enter-active {
  animation: pop 0.24s ease forwards;
}
.pop-leave-active {
  animation: pop 0.2s ease reverse forwards;
}

@keyframes pop {
  0% {
    opacity: 0;
    transform: translateY(20px) scale(0.96);
  }
  100% {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}

@media (max-width: 720px) {
  .chat-panel {
    width: calc(100vw - 20px);
    height: calc(100vh - 32px);
  }

  .console-content {
    grid-template-columns: 1fr;
    grid-template-rows: minmax(0, 1fr) auto;
  }

  .trace-panel {
    max-height: 220px;
    border-top: 1px solid rgba(148, 163, 184, 0.22);
    border-left: 0;
  }
}
</style>
