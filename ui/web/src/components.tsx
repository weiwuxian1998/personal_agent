import type { MessageView, StatusSnapshot } from "@personal-assistant/client";
import { EVENT_TYPE_LABEL, SESSION_STATUS_LABEL } from "./lib.js";

export function SessionBadge({ status }: { status: string }) {
  const color =
    status === "running" ? "blue" :
    status === "done" ? "green" :
    status === "error" ? "red" :
    status === "stopped" || status === "unreachable" ? "orange" : "gray";
  return <span className={`badge ${color}`}>{SESSION_STATUS_LABEL[status] ?? status}</span>;
}

export function EventBadge({ type }: { type: string }) {
  const color =
    type === "user_message" ? "blue" :
    type === "worker_result" ? "green" :
    type === "router_message" ? "purple" :
    type === "worker_message" ? "orange" :
    type === "timer" ? "orange" :
    type === "system" ? "red" : "gray";
  return <span className={`badge ${color}`}>{EVENT_TYPE_LABEL[type] ?? type}</span>;
}

export function Dot({ state }: { state: "ok" | "bad" | "idle" | "warn" }) {
  return <span className={`indicator ${state}`} />;
}

/** router 健康语义：心跳在=运行中；不在但留有历史错误=待机（曾启动失败）；否则=空闲。 */
export function routerHealth(status: StatusSnapshot): { state: "ok" | "bad" | "idle" | "warn"; label: string } {
  if (status.router.alive) return { state: "ok", label: "运行中" };
  if (status.router.state.last_error) return { state: "warn", label: "待机" };
  return { state: "idle", label: "空闲" };
}

export function StatusBar({ status, connected }: { status: StatusSnapshot | null; connected: boolean }) {
  if (!status) return <div className="chips"><span className="chip">连接中…</span></div>;
  const router = routerHealth(status);
  const gw = status.gateway.alive;
  return (
    <div className="chips">
      <span className="chip"><Dot state={gw === true ? "ok" : gw === false ? "bad" : "warn"} />网关 <b>{gw === true ? "在线" : gw === false ? "离线" : "未知"}</b></span>
      <span className="chip"><Dot state={router.state} />Router <b>{router.label}</b></span>
      <span className="chip"><Dot state={status.redis_ok ? "ok" : "bad"} />Redis</span>
      <span className="chip"><Dot state={status.postgres_ok ? "ok" : "bad"} />PG</span>
      <span className="chip">积压 <b>{status.router.stream.lag}</b> / 待确认 <b>{status.router.stream.pending}</b></span>
      <span className="chip">运行中任务 <b>{status.counts.sessions_running}</b></span>
      <span className="chip">活跃定时 <b>{status.counts.schedules_active}</b></span>
      <span className="chip"><Dot state={connected ? "ok" : "bad"} />WS</span>
    </div>
  );
}

/** 侧边栏底部的系统状态卡（宽屏常驻可见）。 */
export function StatusCard({ status, connected }: { status: StatusSnapshot | null; connected: boolean }) {
  const router = status ? routerHealth(status) : null;
  const gw = status?.gateway.alive;
  return (
    <div className="status-card">
      <div className="sc-title">系统状态</div>
      <div className="sc-row"><Dot state={gw === true ? "ok" : gw === false ? "bad" : "warn"} />网关<span className="v">{gw === true ? "在线" : gw === false ? "离线" : "未知"}</span></div>
      <div className="sc-row"><Dot state={router?.state ?? "idle"} />Router<span className="v">{router?.label ?? "…"}</span></div>
      <div className="sc-row"><Dot state={status ? (status.redis_ok ? "ok" : "bad") : "idle"} />Redis<span className="v">{status ? (status.redis_ok ? "正常" : "异常") : "…"}</span></div>
      <div className="sc-row"><Dot state={status ? (status.postgres_ok ? "ok" : "bad") : "idle"} />Postgres<span className="v">{status ? (status.postgres_ok ? "正常" : "异常") : "…"}</span></div>
      <div className="sc-row"><Dot state={connected ? "ok" : "bad"} />WebSocket<span className="v">{connected ? "已连接" : "重连中"}</span></div>
      <div className="sc-sep" />
      <div className="sc-section">运行指标</div>
      <div className="sc-row">积压<span className="v">{status?.router.stream.lag ?? "…"}</span></div>
      <div className="sc-row">待确认<span className="v">{status?.router.stream.pending ?? "…"}</span></div>
      <div className="sc-row">运行中任务<span className="v">{status?.counts.sessions_running ?? "…"}</span></div>
      <div className="sc-row">活跃定时<span className="v">{status?.counts.schedules_active ?? "…"}</span></div>
    </div>
  );
}

export function MessageList({ messages }: { messages: MessageView[] }) {
  if (!messages.length) return <div className="empty">没有消息</div>;
  return (
    <>
      {messages.map((m, i) => (
        <div className="msg" key={m.id || i}>
          <div className="msg-head">
            <span className={`badge ${m.role === "user" ? "blue" : m.role === "tool" ? "gray" : m.role === "system" ? "orange" : "purple"}`}>
              {m.role}
            </span>
            {m.name && <span className="mono dim">{m.name}</span>}
            {m.tool_calls?.map((c, j) => <span className="tool-chip" key={j}>{c.name}</span>)}
            <span className="spacer" />
            {m.usage && (
              <span className="dim small mono">
                in {m.usage.input_tokens.toLocaleString()} / out {m.usage.output_tokens.toLocaleString()}
              </span>
            )}
            {m.truncated && <span className="dim small">截断，共 {m.content_chars?.toLocaleString()} 字</span>}
          </div>
          {m.content && <div className={`msg-body ${m.role === "tool" ? "tool" : ""}`}>{m.content}</div>}
          {m.tool_calls?.filter((c) => c.args_preview).map((c, j) => (
            <details key={j}>
              <summary>{c.name} 参数（{c.args_chars.toLocaleString()} 字符）</summary>
              <pre>{c.args_preview}{c.args_chars > c.args_preview.length ? "\n…（预览截断）" : ""}</pre>
            </details>
          ))}
        </div>
      ))}
    </>
  );
}