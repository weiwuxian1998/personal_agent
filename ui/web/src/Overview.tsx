import { useEffect, useMemo, useRef, useState } from "react";
import type { StatusSnapshot, StreamEvent } from "@personal-assistant/client";
import { client, eventSummary, extractFileRefs, fileName, fileKind, fileUrl, fmtTime, shortId, userText } from "./lib.js";
import { EventBadge } from "./components.js";
import FilePreview from "./FilePreview.js";

interface ChatMsg { key: string; role: "user" | "assistant"; text: string; ts: string; via: string; files: string[] }

function payloadFiles(p: Record<string, unknown>, text: string): string[] {
  const listed = [
    ...(Array.isArray(p.products) ? p.products : []),
    ...(Array.isArray(p.attachments) ? p.attachments : []),
  ].map((v) => String(v)).filter(Boolean);
  return [...new Set([...listed, ...extractFileRefs(text)])];
}

function toChat(e: StreamEvent): ChatMsg | null {
  const p = e.event.payload as Record<string, unknown>;
  if (e.stream === "router" && e.event.type === "user_message") {
    const text = userText(String(p.text ?? ""));
    return {
      key: `r:${e.entry_id}`,
      role: "user",
      text,
      ts: e.event.ts,
      via: p.channel === "web" ? "网页" : "飞书",
      files: payloadFiles(p, text),
    };
  }
  if (e.stream === "outbox" && e.event.type === "router_message") {
    if (p.kind === "web_echo") return null;
    const text = String(p.text ?? "");
    return {
      key: `o:${e.entry_id}`,
      role: "assistant",
      text,
      ts: e.event.ts,
      via: "飞书",
      files: payloadFiles(p, text),
    };
  }
  return null;
}

export default function Overview({ status, events }: { status: StatusSnapshot | null; events: StreamEvent[] }) {
  const [seed, setSeed] = useState<StreamEvent[]>([]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState("");
  const [sendInfo, setSendInfo] = useState("");
  const [preview, setPreview] = useState<string | null>(null);
  const chatBodyRef = useRef<HTMLDivElement>(null);
  const chatId = status?.router.state.last_chat_id ?? "";
  const gatewayOffline = status?.gateway.alive === false;

  function openFile(path: string) {
    if (/^https?:\/\//i.test(path)) {
      window.open(path, "_blank", "noopener,noreferrer");
    } else {
      setPreview(path);
    }
  }

  async function send() {
    const text = draft.trim();
    if (!text || sending) return;
    if (gatewayOffline) {
      setSendError("gateway 离线，先启动 gateway 再发送");
      return;
    }
    setSending(true);
    setSendError("");
    setSendInfo("");
    try {
      const res = await client.sendMessage(text);
      if (!res.ok) {
        setSendError(res.message || res.error || "发送失败");
        return;
      }
      if (res.event && res.entry_id) {
        setSeed((prev) => [
          ...prev,
          { stream: "router", entry_id: res.entry_id!, event: res.event! },
        ]);
      }
      setDraft("");
      if (res.echo && !res.echo.ok) {
        setSendError(`消息已进入处理队列，但手机飞书同步失败：${res.echo.error ?? "未知错误"}`);
      } else {
        setSendInfo("已发送；手机飞书会收到【网页】镜像");
      }
    } catch (err) {
      setSendError(err instanceof Error ? err.message : String(err));
    } finally {
      setSending(false);
    }
  }

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
              对话（网页 ↔ 飞书）
              <span className="badge green">可发送</span>
              <span className="spacer" />
              <span className="dim small mono">{chatId ? `chat ${chatId.slice(-8)}` : "未绑定会话"}</span>
            </div>
            <div className="panel-body" ref={chatBodyRef}>
              {chat.length === 0 && <div className="empty">暂无对话记录</div>}
              {chat.map((m) => (
                <div className={`chat-msg ${m.role}`} key={m.key}>
                  <div className="meta">
                    {m.role === "user" ? "用户" : "助理"} · {m.via} · {fmtTime(m.ts)}
                  </div>
                  <div className="bubble">{m.text}</div>
                  {m.files.length > 0 && (
                    <div className="file-list">
                      {m.files.map((f) =>
                        fileKind(f) === "image" ? (
                          <button type="button" className="file-thumb" key={f} title={f} onClick={() => openFile(f)}>
                            <img src={fileUrl(f)} alt={fileName(f)} />
                          </button>
                        ) : (
                          <button type="button" className="file-chip" key={f} title={f} onClick={() => openFile(f)}>
                            📄 {fileName(f)}
                          </button>
                        ),
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
            <form
              className="composer"
              onSubmit={(e) => {
                e.preventDefault();
                void send();
              }}
            >
              <textarea
                value={draft}
                rows={2}
                placeholder={gatewayOffline ? "gateway 离线，先启动 gateway…" : "输入消息，发送到飞书会话…"}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    void send();
                  }
                }}
                disabled={sending}
              />
              <button
                type="submit"
                className="btn primary"
                disabled={sending || !draft.trim() || gatewayOffline}
              >
                {sending ? "发送中…" : "发送"}
              </button>
            </form>
            {sendError && <div className="composer-error">{sendError}</div>}
            {!sendError && sendInfo && <div className="composer-info">{sendInfo}</div>}
            <div className="composer-hint">
              发送后会同步到飞书；手机端显示为【网页】消息，助理回复也会推送。
            </div>
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
      {preview && <FilePreview path={preview} onClose={() => setPreview(null)} />}
    </div>
  );
}