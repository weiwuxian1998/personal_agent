import { useEffect, useMemo, useRef, useState } from "react";
import type { StatusSnapshot, StreamEvent } from "@personal-assistant/client";
import { client, eventSummary, fmtTime, shortId, userText } from "./lib.js";
import { EventBadge } from "./components.js";

interface ChatMsg { key: string; role: "user" | "assistant"; text: string; ts: string }

function toChat(e: StreamEvent): ChatMsg | null {
  const p = e.event.payload as Record<string, unknown>;
  if (e.stream === "router" && e.event.type === "user_message") {
    return { key: `r:${e.entry_id}`, role: "user", text: userText(String(p.text ?? "")), ts: e.event.ts };
  }
  if (e.stream === "outbox" && e.event.type === "router_message") {
    return { key: `o:${e.entry_id}`, role: "assistant", text: String(p.text ?? ""), ts: e.event.ts };
  }
  return null;
}

export default function Overview({ status, events }: { status: StatusSnapshot | null; events: StreamEvent[] }) {
  const [seed, setSeed] = useState<StreamEvent[]>([]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const chatBodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let alive = true;
    Promise.all([client.events("router", 100), client.events("outbox", 50)])
      .then(([r, o]) => {
        if (!alive) return;
        const items: StreamEvent[] = [
          ...r.items.map((i) => ({ stream: "router" as const, entry_id: i.entry_id, event: i.event })),
          ...o.items.map((i) => ({ stream: "outbox" as const, entry_id: i.entry_id, event: i.event })),
        ];
        setSeed(items);
      })
      .catch(() => undefined);
    return () => { alive = false; };
  }, []);

  const all = useMemo(() => {
    const map = new Map<string, StreamEvent>();
    for (const e of [...seed, ...events]) map.set(`${e.stream}:${e.entry_id}`, e);
    return [...map.values()].sort((a, b) => a.event.ts.localeCompare(b.event.ts));
  }, [seed, events]);

  const chat = useMemo(() => all.map(toChat).filter((m): m is ChatMsg => m !== null), [all]);
  const feed = useMemo(() => [...all].reverse(), [all]);

  useEffect(() => {
    // 只滚聊天面板内部，避免 scrollIntoView 连带滚动外层容器
    const el = chatBodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [chat.length]);

  const chatId = status?.router.state.last_chat_id ?? "";

  return (
    <div className="overview">
      <div className="stats">
        <div className="stat">
          <div className="stat-label">消息积压</div>
          <div className="stat-value">{status?.router.stream.lag ?? 0}</div>
          <div className="stat-sub">待确认 {status?.router.stream.pending ?? 0} · router stream</div>
        </div>
        <div className="stat">
          <div className="stat-label">运行中任务</div>
          <div className="stat-value">{status?.counts.sessions_running ?? 0}</div>
          <div className="stat-sub">worker 进程</div>
        </div>
        <div className="stat">
          <div className="stat-label">活跃定时</div>
          <div className="stat-value">{status?.counts.schedules_active ?? 0}</div>
          <div className="stat-sub">gateway 每 5s 扫描一次</div>
        </div>
      </div>

      <div className="overview-row">
        <div className="chat-panel">
          <div className="panel-box">
            <div className="panel-head">
              对话（飞书会话镜像）
              <span className="badge gray">只读</span>
              <span className="spacer" />
              <span className="dim small mono">{chatId}</span>
            </div>
            <div className="panel-body" ref={chatBodyRef}>
              {chat.length === 0 && <div className="empty">暂无对话记录</div>}
              {chat.map((m) => (
                <div className={`chat-msg ${m.role}`} key={m.key}>
                  <div className="meta">{m.role === "user" ? "用户" : "助理"} · {fmtTime(m.ts)}</div>
                  <div className="bubble">{m.text}</div>
                </div>
              ))}
            </div>
            <div className="chat-hint">只读镜像：对话发生在飞书，本页仅观察，不发送消息。</div>
          </div>
        </div>

        <div className="feed-panel">
          <div className="panel-box">
            <div className="panel-head">实时事件流<span className="spacer" /><span className="dim small">{feed.length} 条 · 点击展开 payload</span></div>
            <div className="panel-body">
              {feed.length === 0 && <div className="empty">暂无事件</div>}
              {feed.map((e) => {
                const key = `${e.stream}:${e.entry_id}`;
                const isOpen = expanded === key;
                const p = e.event.payload as Record<string, unknown>;
                return (
                  <div
                    className={`feed-item ${isOpen ? "expanded" : ""}`}
                    key={key}
                    onClick={() => setExpanded(isOpen ? null : key)}
                  >
                    <span className="t">{fmtTime(e.event.ts)}</span>
                    <EventBadge type={e.event.type} />
                    <span className="dim small">{e.stream === "outbox" ? "→飞书" : e.event.source}</span>
                    {p.task_id ? <span className="mono dim small">{shortId(String(p.task_id))}</span> : null}
                    <span className="txt">{eventSummary(e)}</span>
                    {isOpen && <pre>{JSON.stringify(e.event, null, 2)}</pre>}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}