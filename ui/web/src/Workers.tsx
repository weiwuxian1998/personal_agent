import { useCallback, useEffect, useRef, useState } from "react";
import type { SessionDetail, SessionItem, StreamEvent } from "@personal-assistant/client";
import { client, fmtDateTime, shortId } from "./lib.js";
import { MessageList, SessionBadge } from "./components.js";

export default function Workers({ events }: { events: StreamEvent[] }) {
  const [items, setItems] = useState<SessionItem[]>([]);
  const [total, setTotal] = useState(0);
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const lastEventRef = useRef<string>("");

  const load = useCallback(() => {
    client.sessions({ status: filter || undefined, limit: 100 })
      .then((r) => { setItems(r.items); setTotal(r.total); })
      .catch(() => undefined);
  }, [filter]);

  useEffect(load, [load]);

  // worker 相关事件到达时刷新列表（防抖：同一批事件只刷一次）
  useEffect(() => {
    const last = events[events.length - 1];
    if (!last) return;
    const key = `${last.stream}:${last.entry_id}`;
    if (key === lastEventRef.current) return;
    lastEventRef.current = key;
    if (["worker_result", "worker_notice", "worker_message", "system"].includes(last.event.type)) {
      const t = setTimeout(load, 800);
      return () => clearTimeout(t);
    }
  }, [events, load]);

  useEffect(() => {
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  return (
    <div className="workers-layout">
      <div className="workers-list">
        <div className="panel-box">
          <div className="panel-head">
            Worker 会话（{total}）
            <span className="spacer" />
            <select value={filter} onChange={(e) => setFilter(e.target.value)}>
              <option value="">全部状态</option>
              <option value="running">运行中</option>
              <option value="done">完成</option>
              <option value="error">错误</option>
              <option value="stopped">已停止</option>
            </select>
            <button className="btn" onClick={load}>刷新</button>
          </div>
          <div className="panel-body">
            <table className="tbl">
              <thead>
                <tr><th>任务</th><th>状态</th><th>进程</th><th>更新时间</th></tr>
              </thead>
              <tbody>
                {items.map((s) => (
                  <tr
                    key={s.id}
                    className={`clickable ${selected === s.id ? "selected" : ""}`}
                    onClick={() => setSelected(s.id)}
                  >
                    <td title={s.prompt_preview || s.title}>
                      {s.title || shortId(s.id)}
                      {s.task_kind && s.task_kind !== "general" && <span className="badge gray" style={{ marginLeft: 6 }}>{s.task_kind}</span>}
                    </td>
                    <td><SessionBadge status={s.status} /></td>
                    <td>{s.pid_alive ? <span className="badge blue">pid {s.pid}</span> : <span className="dim small">已退出</span>}</td>
                    <td className="dim small">{fmtDateTime(s.updated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {items.length === 0 && <div className="empty">没有会话</div>}
          </div>
        </div>
      </div>

      <div className="workers-detail">
        {selected
          ? <Detail key={selected} id={selected} />
          : <div className="panel-box"><div className="empty">← 选择一个任务查看详情</div></div>}
      </div>
    </div>
  );
}

function Detail({ id }: { id: string }) {
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [older, setOlder] = useState<SessionDetail["context"]["messages"]>([]);
  const [log, setLog] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setOlder([]);
    client.session(id, { limit: 30, includeTools: true })
      .then(setDetail)
      .catch(() => undefined)
      .finally(() => setLoading(false));
  }, [id]);

  useEffect(load, [load]);

  async function loadEarlier() {
    const cursor = detail?.context.next_cursor;
    if (!cursor || !detail) return;
    const r = await client.session(id, { cursor, limit: 30, includeTools: true });
    setOlder((prev) => [...r.context.messages, ...prev]);
    setDetail({ ...detail, context: { ...detail.context, next_cursor: r.context.next_cursor } });
  }

  async function loadLog() {
    const r = await client.sessionLog(id, 131072);
    setLog(r.ok ? (r.text ?? "") : `日志不可用：${r.error}`);
  }

  if (!detail) return <div className="panel-box"><div className="empty">{loading ? "加载中…" : "无数据"}</div></div>;
  const s = detail.session;
  const ctx = detail.context;
  const messages = [...older, ...ctx.messages];

  return (
    <div className="panel-box">
      <div className="panel-head">
        <span className="mono">{s.id}</span>
        <SessionBadge status={s.status} />
        <span className="spacer" />
        <button className="btn" onClick={load} disabled={loading}>刷新</button>
        <button className="btn" onClick={() => void loadLog()}>日志尾部</button>
      </div>
      <div className="panel-body">
        <div className="detail-section">
          <div className="kv">
            <span className="k">标题</span><span className="v">{s.title}</span>
            <span className="k">类型</span><span className="v">{s.task_kind ?? "general"}</span>
            <span className="k">进程</span><span className="v">{s.pid ?? "-"}（{s.pid_alive ? "存活" : "已退出"}）</span>
            <span className="k">cwd</span><span className="v">{s.cwd ?? "-"}</span>
            <span className="k">创建</span><span className="v">{fmtDateTime(s.created_at)}</span>
            <span className="k">首次回报</span><span className="v">{fmtDateTime(s.first_message_at)}</span>
            <span className="k">last_seen</span><span className="v">{fmtDateTime(s.last_seen)}</span>
          </div>
        </div>
        {s.prompt_preview && (
          <div className="detail-section">
            <h4>任务 prompt（预览）</h4>
            <pre>{s.prompt_preview}</pre>
          </div>
        )}
        {s.result_preview && (
          <div className="detail-section">
            <h4>最终结果（result.json · {s.result_status}）</h4>
            <pre>{s.result_preview}</pre>
          </div>
        )}
        {log !== null && (
          <div className="detail-section">
            <h4>日志尾部</h4>
            <pre>{log}</pre>
          </div>
        )}
        <div className="detail-section">
          <h4>
            会话消息（{ctx.source === "checkpoint" ? `checkpoint · 共 ${ctx.total} 条` : ctx.source}）
            {ctx.next_cursor && <button className="btn" style={{ marginLeft: 8 }} onClick={() => void loadEarlier()}>加载更早 ↑</button>}
          </h4>
          {ctx.note && <div className="dim small" style={{ marginBottom: 8 }}>{ctx.note}</div>}
          <MessageList messages={messages} />
        </div>
      </div>
    </div>
  );
}