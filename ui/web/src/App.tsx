import { useState } from "react";
import type { ReactNode } from "react";
import { useAssistant, fmtTime, fmtDateTime } from "./lib.js";
import { StatusBar, StatusCard } from "./components.js";
import Overview from "./Overview.js";
import RouterContext from "./RouterContext.js";
import Workers from "./Workers.js";
import Schedules from "./Schedules.js";
import Costs from "./Costs.js";

type Tab = "overview" | "router" | "workers" | "schedules" | "costs";

const TABS: { key: Tab; label: string }[] = [
  { key: "overview", label: "总览 / 对话" },
  { key: "router", label: "Router 上下文" },
  { key: "workers", label: "Workers" },
  { key: "schedules", label: "定时任务" },
  { key: "costs", label: "费用" },
];

const svgProps = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.7,
  strokeLinecap: "round",
  strokeLinejoin: "round",
} as const;

const ICONS: Record<Tab, ReactNode> = {
  overview: (
    <svg {...svgProps}><path d="M21 11.5a8.38 8.38 0 0 1-8.5 8.5c-1.5 0-3-.4-4.3-1.1L3 20l1.1-5.2A8.5 8.5 0 1 1 21 11.5z" /></svg>
  ),
  router: (
    <svg {...svgProps}><path d="M12 2 2 7l10 5 10-5-10-5z" /><path d="M2 17l10 5 10-5" /><path d="M2 12l10 5 10-5" /></svg>
  ),
  workers: (
    <svg {...svgProps}><rect x="5" y="5" width="14" height="14" rx="2" /><rect x="10" y="10" width="4" height="4" /><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3" /></svg>
  ),
  schedules: (
    <svg {...svgProps}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 3" /></svg>
  ),
  costs: (
    <svg {...svgProps}><path d="M3 3v18h18" /><path d="M7 14l4-4 3 3 5-6" /></svg>
  ),
};

export default function App() {
  const [tab, setTab] = useState<Tab>("overview");
  const { status, events, connected } = useAssistant();
  const lastError = status?.router.state.last_error;
  const showError = !!lastError && !status?.router.alive;

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">
            <svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l2.4 7.6L22 12l-7.6 2.4L12 22l-2.4-7.6L2 12l7.6-2.4z" /></svg>
          </span>
          <div>
            <div className="brand-name">个人助理控制台</div>
            <div className="brand-sub">personal-assistant</div>
          </div>
        </div>
        <nav className="nav">
          {TABS.map((t) => (
            <button key={t.key} className={`nav-item ${tab === t.key ? "active" : ""}`} onClick={() => setTab(t.key)}>
              {ICONS[t.key]}
              {t.label}
            </button>
          ))}
        </nav>
        <StatusCard status={status} connected={connected} />
      </aside>

      <div className="main-col">
        <header className="header">
          <h1>{TABS.find((t) => t.key === tab)?.label}</h1>
          <span className="spacer" />
          <div className="header-chips"><StatusBar status={status} connected={connected} /></div>
          <span className="chip">渠道 <b>{status?.channel_kind ?? "…"}</b></span>
          <span className="clock">{status ? fmtTime(status.ts) : "--:--:--"}</span>
        </header>

        {showError && (
          <details className="error-banner">
            <summary>
              Router 上次启动失败（{fmtDateTime(status?.router.state.updated_at ?? "")}）· 当前待机，新消息到达时会重试 —— 点击展开 traceback
            </summary>
            <pre>{lastError}</pre>
          </details>
        )}

        <main className="main">
          {tab === "overview" && <Overview status={status} events={events} />}
          {tab === "router" && <RouterContext />}
          {tab === "workers" && <Workers events={events} />}
          {tab === "schedules" && <Schedules />}
          {tab === "costs" && <Costs />}
        </main>
      </div>
    </div>
  );
}