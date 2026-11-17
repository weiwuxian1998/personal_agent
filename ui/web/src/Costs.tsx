import { useCallback, useEffect, useState } from "react";
import type { CostItem, CostTotal } from "@personal-assistant/client";
import { client, fmtDateTime } from "./lib.js";

export default function Costs() {
  const [items, setItems] = useState<CostItem[]>([]);
  const [totals, setTotals] = useState<CostTotal[]>([]);

  const load = useCallback(() => {
    client.costs(100).then((r) => { setItems(r.items); setTotals(r.totals); }).catch(() => undefined);
  }, []);

  useEffect(load, [load]);

  const grand = totals.reduce(
    (acc, t) => ({
      calls: acc.calls + t.calls,
      input: acc.input + t.input_tokens,
      cached: acc.cached + t.cached_tokens,
      output: acc.output + t.output_tokens,
      usd: acc.usd + t.cost_usd,
    }),
    { calls: 0, input: 0, cached: 0, output: 0, usd: 0 },
  );

  return (
    <div className="view">
      <div className="toolbar">
        <span className="title">Token / 费用账本</span>
        <button className="btn" onClick={load}>刷新</button>
        <span className="dim small">
          累计 {grand.calls} 次调用 · 输入 {grand.input.toLocaleString()}（缓存命中 {grand.cached.toLocaleString()}）· 输出 {grand.output.toLocaleString()} · ${grand.usd.toFixed(4)}
        </span>
      </div>

      <div className="card" style={{ padding: 0 }}>
        <table className="tbl">
          <thead><tr><th>范围</th><th>模型</th><th className="num">调用</th><th className="num">输入</th><th className="num">缓存命中</th><th className="num">输出</th><th className="num">费用 USD</th></tr></thead>
          <tbody>
            {totals.map((t) => (
              <tr key={`${t.scope}:${t.model}`}>
                <td><span className={`badge ${t.scope === "router" ? "purple" : "blue"}`}>{t.scope}</span></td>
                <td className="mono small">{t.model}</td>
                <td className="num">{t.calls}</td>
                <td className="num">{t.input_tokens.toLocaleString()}</td>
                <td className="num">{t.cached_tokens.toLocaleString()}</td>
                <td className="num">{t.output_tokens.toLocaleString()}</td>
                <td className="num">{t.cost_usd.toFixed(4)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {totals.length === 0 && <div className="empty">暂无账目</div>}
      </div>

      <div className="card" style={{ padding: 0 }}>
        <table className="tbl">
          <thead><tr><th>时间</th><th>范围</th><th>模型</th><th className="num">输入</th><th className="num">输出</th><th className="num">USD</th><th>trace</th></tr></thead>
          <tbody>
            {items.map((c) => (
              <tr key={c.id}>
                <td className="small dim">{fmtDateTime(c.ts_utc)}</td>
                <td className="small">{c.scope}</td>
                <td className="mono small">{c.model}</td>
                <td className="num">{c.input_tokens.toLocaleString()}</td>
                <td className="num">{c.output_tokens.toLocaleString()}</td>
                <td className="num">{c.cost_usd.toFixed(4)}</td>
                <td className="mono small dim">{c.trace_id ? c.trace_id.slice(-8) : "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {items.length === 0 && <div className="empty">暂无记录</div>}
      </div>
    </div>
  );
}