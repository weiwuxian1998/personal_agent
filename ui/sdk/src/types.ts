/**
 * 协议类型定义：与 personal-assistant/contracts.py、bus/envelope.py、
 * protocol_adapter.py 的返回结构一一对齐（唯一真相仍在 Python 侧）。
 */

// ---------------------------------------------------------------- Event 信封
export type EventType =
  | "user_message"
  | "worker_result"
  | "worker_message"
  | "worker_notice"
  | "router_message"
  | "timer"
  | "system";

export type EventSource = "feishu" | "console" | "worker" | "scheduler" | "router" | "system";

export interface EventEnvelope {
  type: EventType;
  source: EventSource;
  payload: Record<string, unknown>;
  v: number;
  id: string;
  ts: string;
  trace_id: string;
  dedupe_key: string | null;
}

export interface StreamEvent {
  stream: "router" | "outbox";
  entry_id: string;
  event: EventEnvelope;
}

// ---------------------------------------------------------------- SQLite 行
export type SessionStatus =
  | "running" | "waiting_router" | "idle" | "stopped" | "error" | "done" | "unreachable";

export interface SessionItem {
  id: string;
  title: string;
  status: SessionStatus;
  cwd: string | null;
  pid: number | null;
  pid_started_at: string | null;
  parent_id: string | null;
  created_at: string;
  updated_at: string;
  first_message_at: string | null;
  last_seen?: string | null;
  pid_alive: boolean;
  prompt_preview?: string;
  task_kind?: string;
  chat_id?: string;
  result_status?: string;
  result_preview?: string;
}

export interface ScheduleItem {
  id: string;
  cron: string | null;
  at: string | null;
  timezone: string;
  prompt: string;
  status: string;
  next_run_utc: string | null;
  last_run_utc: string | null;
  misfire_grace_s: number;
  catch_up: string;
  created_at: string;
}

export interface WatchItem {
  task_id: string;
  reason: string;
  status: string;
  first_detected_at: string;
  last_detected_at: string;
  alerted_at: string | null;
  dedupe_key: string;
}

// ---------------------------------------------------------------- 状态快照
export interface StreamMetrics {
  xlen: number;
  lag: number;
  pending: number;
}

export interface StatusSnapshot {
  ts: string;
  redis_ok: boolean;
  postgres_ok: boolean;
  channel_kind: string;
  gateway: { alive: boolean | null };
  router: {
    alive: boolean;
    heartbeat: string | null;
    state: Record<string, string>;
    stream: StreamMetrics;
  };
  outbox: { stream: StreamMetrics };
  counts: { sessions_running: number; schedules_active: number };
}

// ---------------------------------------------------------------- checkpoint 消息视图
export interface ToolCallView {
  name: string;
  id: string;
  args_preview: string;
  args_chars: number;
}

export interface MessageView {
  role: "user" | "assistant" | "tool" | "system";
  id?: string;
  content: string;
  truncated?: boolean;
  content_chars?: number;
  tool_calls?: ToolCallView[];
  tool_call_id?: string;
  name?: string;
  usage?: { input_tokens: number; output_tokens: number };
}

export interface ContextPage {
  ok: boolean;
  thread_id: string;
  source: "checkpoint" | "result_json" | "none";
  note?: string;
  checkpoint?: { id: string; ts: string; step: number };
  total: number;
  returned: number;
  total_chars?: number;
  next_cursor: string | null;
  messages: MessageView[];
}

export interface SessionDetail {
  ok: boolean;
  session: SessionItem;
  context: ContextPage;
}

// ---------------------------------------------------------------- JSONL 记账
export interface TurnToolCall {
  name: string;
  args_hash: string;
  ok: boolean;
  ms: number;
}

export interface TurnRecord {
  v?: number;
  seq?: number;
  ts?: string;
  trace_id: string;
  chat_id: string;
  event_ids: string[];
  digest: string;
  reply: string;
  tool_calls: TurnToolCall[];
  products?: string[];
  ok: boolean;
}

export interface InboxRecord {
  seq?: number;
  ts?: string;
  event_id: string;
  type: EventType;
  source: EventSource;
  trace_id: string;
  payload: Record<string, unknown>;
  entry_ts?: string;
}

// ---------------------------------------------------------------- REST 返回
export interface SessionsPage { ok: boolean; total: number; items: SessionItem[] }
export interface ListResponse<T> { ok: boolean; items: T[] }
export interface EventsResponse { ok: boolean; stream: string; items: { entry_id: string; event: EventEnvelope }[] }
export interface LogResponse { ok: boolean; path?: string; size?: number; truncated?: boolean; text?: string; error?: string }

// ---------------------------------------------------------------- WS 帧
export type ServerFrame =
  | { type: "hello"; status: StatusSnapshot; events: StreamEvent[] }
  | ({ type: "event" } & StreamEvent)
  | { type: "status"; status: StatusSnapshot }
  | { type: "pong" };

export type ClientFrame =
  | { type: "ping" };

export interface PageOpts {
  cursor?: string | null;
  limit?: number;
  includeTools?: boolean;
}