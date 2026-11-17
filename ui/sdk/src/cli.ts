#!/usr/bin/env node
/**
 * 协议验证 CLI：用 SDK 连接真实适配器，验证只读 REST / WS 协议。
 *
 * 用法：
 *   node dist/cli.js status [--url http://127.0.0.1:8765]
 *   node dist/cli.js sessions [--limit 10]
 *   node dist/cli.js schedules
 *   node dist/cli.js context [--limit 10]
 *   node dist/cli.js turns [--limit 5]
 *   node dist/cli.js events [--stream router|outbox] [--count 10]
 *   node dist/cli.js watch [--seconds 30]          # WS 实时帧
 */

import { AssistantClient } from "./client.js";
import type { StreamEvent } from "./types.js";

function arg(name: string, fallback = ""): string {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

function briefEvent(e: StreamEvent): string {
  const p = e.event.payload as Record<string, unknown>;
  const text = String(p.text ?? p.detail ?? p.prompt ?? "");
  return `[${e.stream}] ${e.event.ts} ${e.event.type}/${e.event.source} ` +
    `${p.task_id ? `task=${String(p.task_id).slice(-8)} ` : ""}` +
    `${text.slice(0, 120).replace(/\n/g, " ")}`;
}

async function main(): Promise<number> {
  const cmd = process.argv[2] ?? "status";
  const client = new AssistantClient({ baseUrl: arg("url", "http://127.0.0.1:8765") });

  switch (cmd) {
    case "status": {
      const s = await client.status();
      console.log(JSON.stringify(s, null, 2));
      return 0;
    }
    case "sessions": {
      const r = await client.sessions({ limit: Number(arg("limit", "10")) });
      console.log(`total=${r.total}`);
      for (const s of r.items) {
        console.log(`${s.id}  ${s.status.padEnd(8)} alive=${s.pid_alive}  ${s.title}`);
      }
      return 0;
    }
    case "schedules": {
      const r = await client.schedules();
      for (const s of r.items) {
        console.log(`${s.id}  ${s.status.padEnd(8)} cron=${s.cron ?? "-"} at=${s.at ?? "-"} next=${s.next_run_utc ?? "-"}`);
        console.log(`    ${s.prompt.slice(0, 100)}`);
      }
      return 0;
    }
    case "context": {
      const r = await client.routerContext({ limit: Number(arg("limit", "10")) });
      console.log(`source=${r.source} total=${r.total} returned=${r.returned} next_cursor=${r.next_cursor}`);
      for (const m of r.messages) {
        const calls = m.tool_calls?.map((c) => c.name).join(",");
        console.log(`--- ${m.role}${calls ? ` [tools: ${calls}]` : ""}`);
        console.log(m.content.slice(0, 300));
      }
      return 0;
    }
    case "turns": {
      const r = await client.turns(Number(arg("limit", "5")));
      for (const t of r.items) {
        console.log(`seq=${t.seq} ts=${t.ts} tools=[${t.tool_calls.join(",")}]`);
        console.log(`  digest: ${t.digest.slice(0, 150).replace(/\n/g, " | ")}`);
        console.log(`  reply : ${t.reply.slice(0, 150)}`);
      }
      return 0;
    }
    case "events": {
      const r = await client.events(arg("stream", "router") as "router" | "outbox", Number(arg("count", "10")));
      for (const item of r.items) {
        console.log(briefEvent({ stream: r.stream as "router" | "outbox", entry_id: item.entry_id, event: item.event }));
      }
      return 0;
    }
    case "watch": {
      const seconds = Number(arg("seconds", "30"));
      client.on("hello", ({ status, events }) => {
        console.log(`# hello: router.alive=${status.router.alive} gateway.alive=${status.gateway.alive} lag=${status.router.stream.lag} replay=${events.length}`);
        for (const e of events.slice(-5)) console.log(briefEvent(e));
      });
      client.on("event", (e) => console.log(briefEvent(e)));
      client.on("status", (s) => console.log(`# status: router.alive=${s.router.alive} lag=${s.router.stream.lag} running=${s.counts.sessions_running}`));
      client.on("open", () => console.log("# ws open"));
      client.on("close", ({ code }) => console.log(`# ws close code=${code}（自动重连中）`));
      client.connect();
      await new Promise((r) => setTimeout(r, seconds * 1000));
      client.disconnect();
      return 0;
    }
    default:
      console.error(`未知命令: ${cmd}（status|sessions|schedules|context|turns|events|watch）`);
      return 1;
  }
}

main().then(
  (code) => process.exit(code),
  (err) => {
    console.error("CLI 失败:", err);
    process.exit(1);
  },
);