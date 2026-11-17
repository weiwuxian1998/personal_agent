import { useCallback, useEffect, useState } from "react";
import type { ScheduleItem, WatchItem } from "@personal-assistant/client";
import { client, fmtDateTime, fmtRelative } from "./lib.js";

export default function Schedules() {
  const [items, setItems] = useState<ScheduleItem[]>([]);
  const [watches, setWatches] = useState<WatchItem[]>([]);

  const load = useCallback(() => {
    client.schedules().then((r) => setItems(r.items)).catch(() => undefined);
    client.watches().then((r) => setWatches(r.items)).catch(() => undefined);
  }, []);

  useEffect(load, [load]);
  useEffect(() => {
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [load]);

  return (
    <div className="view">
      <div className="toolbar">
        <span className="title">定时任务</span>
        <button className="btn" onClick={load}>刷新</button>
        <span className="dim small">只读视图 · 增删改请直接在对话里让助理操作（Schedule 工具）</span>
      </div>
      <div className="card" style={{ padding: 0 }}>
        <table className="tbl">
          <thead>
            <tr><th>内容</th><th>触发规则</th><th>状态</th><th>下次运行</th><th>上次运行</th></tr>
          </thead>
          <tbody>
            {items.map((s) => (
              <tr key={s.id}>
                <td style={{ maxWidth: 460 }}>
                  <details>
                    <summary style={{ cursor: "pointer" }}>{s.prompt.slice(0, 60)}{s.prompt.length > 60 ? "…" : ""}</summary>
                    <pre className="mono small dim" style={{ whiteSpace: "pre-wrap", maxHeight: 260, overflow: "auto" }}>{s.prompt}</pre>
                  </details>
                </td>
                <td className="mono small">
                  {s.cron ? <>cron <b>{s.cron}</b></> : <>一次性 {fmtDateTime(s.at)}</>}
                  <div className="dim">{s.timezone}</div>
                </td>
                <td>
                  <span className={`badge ${s.status === "active" ? "green" : s.status === "paused" ? "orange" : "gray"}`}>{s.status}</span>
                </td>
                <td className="small">
                  {s.next_run_utc ? (
                    <>{fmtDateTime(s.next_run_utc)} <span className="dim">（{fmtRelative(s.next_run_utc)}）</span></>
                  ) : "-"}
                </td>
                <td className="small dim">{fmtDateTime(s.last_run_utc)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {items.length === 0 && <div className="empty">没有定时任务</div>}
      </div>

      <div className="toolbar" style={{ marginTop: 16 }}>
        <span className="title">巡检告警（task_watch）</span>
        <span className="dim small">worker 卡住 / 进程消失时由 watchdog 记录，router 会来问你怎么处理</span>
      </div>
      <div className="card" style={{ padding: 0 }}>
        <table className="tbl">
          <thead><tr><th>任务</th><th>原因</th><th>状态</th><th>首次发现</th><th>最近发现</th></tr></thead>
          <tbody>
            {watches.map((w) => (
              <tr key={w.dedupe_key}>
                <td className="mono small">{w.task_id}</td>
                <td><span className={`badge ${w.reason === "process_gone" ? "red" : "orange"}`}>{w.reason}</span></td>
                <td className="small">{w.status}</td>
                <td className="small dim">{fmtDateTime(w.first_detected_at)}</td>
                <td className="small dim">{fmtDateTime(w.last_detected_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {watches.length === 0 && <div className="empty">暂无告警</div>}
      </div>
    </div>
  );
}