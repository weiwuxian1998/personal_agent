import { useEffect, useState } from "react";
import { AssistantClient } from "@personal-assistant/client";
import type { StatusSnapshot, StreamEvent } from "@personal-assistant/client";

/** 全局单例：整个应用共用一条 WS 连接（SDK 内部自动重连 + 回放去重）。 */
export const client = new AssistantClient();

const MAX_EVENTS = 400;

export interface AssistantState {
  status: StatusSnapshot | null;
  events: StreamEvent[];
  connected: boolean;
}

export function useAssistant(): AssistantState {
  const [status, setStatus] = useState<StatusSnapshot | null>(null);
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const offs = [
      client.on("hello", ({ status, events }) => {
        setStatus(status);
        setEvents(events.slice(-MAX_EVENTS));
      }),
      client.on("status", (s) => setStatus(s)),
      client.on("event", (e) => setEvents((prev) => [...prev, e].slice(-MAX_EVENTS))),
      client.on("open", () => setConnected(true)),
      client.on("close", () => setConnected(false)),
    ];
    client.connect();
    return () => offs.forEach((off) => off());
  }, []);

  return { status, events, connected };
}

// ---------------------------------------------------------------- 格式化
export function fmtTime(iso?: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}

export function fmtDateTime(iso?: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

/** next_run_utc → 「x 分钟后 / 已过期」之类的相对描述 */
export function fmtRelative(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const diffMs = d.getTime() - Date.now();
  const abs = Math.abs(diffMs);
  const mins = Math.round(abs / 60000);
  const label = mins < 60 ? `${mins} 分钟` : mins < 60 * 48 ? `${(mins / 60).toFixed(1)} 小时` : `${(mins / 1440).toFixed(1)} 天`;
  return diffMs >= 0 ? `${label}后` : `已过期 ${label}`;
}

/** 从 gateway 包装文本里提取用户原话 */
export function userText(raw: string): string {
  const m = /<user_query>([\s\S]*?)<\/user_query>/.exec(raw ?? "");
  return m ? m[1] : (raw ?? "");
}

export function shortId(id?: string | null): string {
  return id ? id.slice(-8) : "";
}

export function eventSummary(e: StreamEvent): string {
  const p = e.event.payload as Record<string, unknown>;
  if (e.event.type === "user_message") return userText(String(p.text ?? ""));
  if (e.event.type === "system") return String(p.text ?? p.reason ?? p.kind ?? "");
  return String(p.text ?? p.detail ?? p.prompt ?? "");
}

// ---------------------------------------------------------------- 文件引用 / 预览
const PREVIEW_TEXT_EXT = new Set([
  "md", "markdown", "txt", "json", "jsonl", "log", "py", "ts", "tsx", "js", "jsx",
  "css", "html", "yml", "yaml", "csv", "tsv", "sql", "ini", "toml", "sh", "ps1",
]);

const PREVIEW_IMAGE_EXT = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "svg"]);
const PREVIEW_VIDEO_EXT = new Set(["mp4", "webm", "mov"]);
const PREVIEW_AUDIO_EXT = new Set(["mp3", "wav", "ogg", "m4a"]);

function fileExt(path: string): string {
  const name = path.split(/[\\/]/).pop() ?? "";
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
}

export function fileName(path: string): string {
  return path.split(/[\\/]/).pop() || path;
}

/** 本地文件走 adapter 的只读预览接口；http(s) 原样返回。 */
export function fileUrl(path: string, download = false): string {
  if (/^https?:\/\//i.test(path)) return path;
  const u = `/api/files?path=${encodeURIComponent(path)}`;
  return download ? `${u}&download=true` : u;
}

export function fileKind(path: string): "image" | "pdf" | "video" | "audio" | "text" | "other" {
  const ext = fileExt(path);
  if (PREVIEW_IMAGE_EXT.has(ext)) return "image";
  if (ext === "pdf") return "pdf";
  if (PREVIEW_VIDEO_EXT.has(ext)) return "video";
  if (PREVIEW_AUDIO_EXT.has(ext)) return "audio";
  if (PREVIEW_TEXT_EXT.has(ext)) return "text";
  return "other";
}

/** 从回复正文里提取被提及的本地文件路径（products/attachments 之外的兜底）。 */
export function extractFileRefs(text: string): string[] {
  const found = new Set<string>();
  const backtick = /`([^`\n]+)`/g;
  let m: RegExpExecArray | null;
  while ((m = backtick.exec(text)) !== null) {
    const raw = m[1].trim();
    if (raw) found.add(raw);
  }
  // Windows 绝对路径；排除中文标点和常见句末符号，避免把「。报告」带进去
  const winPath = /(?:^|[\s`"'（(])([A-Za-z]:\\[^\s`"'<>|（）()，。；、]+)/g;
  while ((m = winPath.exec(text)) !== null) {
    const raw = m[1].replace(/[.,;:]+$/, "");
    if (raw) found.add(raw);
  }
  return [...found].filter((p) => {
    if (/^https?:\/\//i.test(p)) return false;
    return fileExt(p) !== "" && (PREVIEW_TEXT_EXT.has(fileExt(p)) || PREVIEW_IMAGE_EXT.has(fileExt(p)) || fileExt(p) === "pdf");
  });
}

export const EVENT_TYPE_LABEL: Record<string, string> = {
  user_message: "用户",
  worker_result: "结果",
  worker_message: "提问",
  worker_notice: "通知",
  router_message: "回复",
  timer: "定时",
  system: "系统",
};

export const SESSION_STATUS_LABEL: Record<string, string> = {
  running: "运行中",
  waiting_router: "等router",
  idle: "空闲",
  stopped: "已停止",
  error: "错误",
  done: "完成",
  unreachable: "失联",
};