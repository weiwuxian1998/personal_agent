import { useCallback, useEffect, useState } from "react";
import type { ContextPage, TurnRecord } from "@personal-assistant/client";
import { client, fmtDateTime } from "./lib.js";
import { MessageList } from "./components.js";

export default function RouterContext() {
  const [sub, setSub] = useState<"turns" | "messages">("turns");
  return (
    <div className="view">
      <div className="toolbar">
        <span className="title">Router 上下文</span>
        <button className={`btn ${sub === "turns" ? "primary" : ""}`} onClick={() => setSub("turns")}>每轮记账（turns.jsonl）</button>
        <button className={`btn ${sub === "messages" ? "primary" : ""}`} onClick={() => setSub("messages")}>完整消息（checkpoint）</button>
        <span className="dim small">thread: router:main · 只读，不影响 router 运行</span>
      </div>
      {sub === "turns" ? <Turns /> : <Messages />}
    </div>
  );
}

function Turns() {
  const [items, setItems] = useState<TurnRecord[]>([]);
  const [loading, setLoading] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    client.turns(100)
      .then((r) => setItems(r.items))
      .catch(() => undefined)
      .finally(() => setLoading(false));
  }, []);

  useEffect(load, [load]);

  return (
    <>
      <div className="toolbar">
        <button className="btn" onClick={load} disabled={loading}>{loading ? "刷新中…" : "刷新"}</button>
        <span className="dim small">{items.length} 轮 · 每轮 = 一次唤醒事件批 → 模型一轮 → ACK</span>
      </div>
      {items.length === 0 && <div className="empty">暂无记账</div>}
      {items.map((t) => (
        <div className="turn-card" key={t.seq ?? t.trace_id}>
          <div className="turn-head">
            <span className="mono dim">#{t.seq}</span>
            <span className="dim">{fmtDateTime(t.ts)}</span>
            {t.tool_calls.map((c, i) => (
              <span
                className="tool-chip"
                key={i}
                title={`${c.args_hash} · ${c.ms}ms · ${c.ok ? "ok" : "failed"}`}
                style={{ borderColor: c.ok ? undefined : "var(--red-bd)", color: c.ok ? undefined : "var(--red)" }}
              >
                {c.name}{c.ms > 0 ? <span className="dim" style={{ marginLeft: 4 }}>·{c.ms}ms</span> : ""}
              </span>
            ))}
            {t.tool_calls.length === 0 && <span className="dim small">无工具调用</span>}
            <span className="spacer" />
            <span className="dim small mono">{t.chat_id ? t.chat_id.slice(0, 12) + "…" : ""}</span>
          </div>
          <div className="turn-reply">{t.reply || <span className="dim">（本轮未回复用户）</span>}</div>
          <details>
            <summary>唤醒摘要 digest（{t.digest.length.toLocaleString()} 字符）</summary>
            <pre>{t.digest}</pre>
          </details>
        </div>
      ))}
    </>
  );
}

function Messages() {
  const [page, setPage] = useState<ContextPage | null>(null);
  const [older, setOlder] = useState<ContextPage["messages"]>([]);
  const [includeTools, setIncludeTools] = useState(true);
  const [loading, setLoading] = useState(false);

  const load = useCallback((tools: boolean) => {
    setLoading(true);
    setOlder([]);
    client.routerContext({ limit: 50, includeTools: tools })
      .then(setPage)
      .catch(() => undefined)
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(includeTools); }, [includeTools, load]);

  async function loadEarlier() {
    if (!page?.next_cursor) return;
    setLoading(true);
    try {
      const r = await client.routerContext({ cursor: page.next_cursor, limit: 50, includeTools });
      setOlder((prev) => [...r.messages, ...prev]);
      setPage({ ...page, next_cursor: r.next_cursor });
    } finally {
      setLoading(false);
    }
  }

  const messages = [...older, ...(page?.messages ?? [])];

  return (
    <>
      <div className="toolbar">
        <button className="btn" onClick={() => load(includeTools)} disabled={loading}>刷新</button>
        <label className="dim small">
          <input type="checkbox" checked={includeTools} onChange={(e) => setIncludeTools(e.target.checked)} /> 显示工具消息
        </label>
        {page && (
          <span className="dim small">
            共 {page.total} 条 · 已显示 {messages.length} 条
            {page.checkpoint && <> · checkpoint step {page.checkpoint.step} · {fmtDateTime(page.checkpoint.ts)}</>}
          </span>
        )}
        <span className="spacer" />
        {page?.next_cursor && <button className="btn" onClick={() => void loadEarlier()} disabled={loading}>加载更早 ↑</button>}
      </div>
      {page?.source === "none" && <div className="card dim">{page.note}</div>}
      <MessageList messages={messages} />
    </>
  );
}