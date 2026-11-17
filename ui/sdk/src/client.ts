/**
 * AssistantClient：REST 封装 + WebSocket 长连接（自动重连、回放去重）。
 *
 * 零运行时依赖：fetch / WebSocket 用运行时全局（浏览器与 Node ≥ 22 均内置）。
 */

import type {
  ClientFrame, ContextPage, CostsResponse, EventsResponse, ListResponse,
  LogResponse, PageOpts, ScheduleItem, ServerFrame, SessionsPage,
  SessionDetail, StatusSnapshot, StreamEvent, TurnRecord, InboxRecord, WatchItem,
} from "./types.js";

export interface ClientOptions {
  /**适配器地址；浏览器里缺省用当前页面 origin */
  baseUrl?: string;
  /**WS 地址；缺省由 baseUrl 推导（http→ws + /ws） */
  wsUrl?: string;
  /**断线自动重连，默认 true */
  reconnect?: boolean;
  /**注入 WebSocket 实现（测试用） */
  webSocketImpl?: typeof WebSocket;
}

export interface EventMap {
  open: undefined;
  close: { code: number; reason: string };
  error: Error;
  hello: { status: StatusSnapshot; events: StreamEvent[] };
  event: StreamEvent;
  status: StatusSnapshot;
  pong: undefined;
}

type Listener<K extends keyof EventMap> = (payload: EventMap[K]) => void;

const REPLAY_DEDUPE_MAX = 2000;
const BACKOFF_MIN_MS = 1000;
const BACKOFF_MAX_MS = 15000;

function defaultBaseUrl(): string {
  if (typeof location !== "undefined" && location.origin) return location.origin;
  return "http://127.0.0.1:8765";
}

export class AssistantClient {
  readonly baseUrl: string;
  readonly wsUrl: string;
  private readonly reconnectEnabled: boolean;
  private readonly wsImpl: typeof WebSocket;
  private ws: WebSocket | null = null;
  private disposed = false;
  private backoffMs = BACKOFF_MIN_MS;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private seen = new Set<string>();
  private seenOrder: string[] = [];
  private listeners: { [K in keyof EventMap]?: Set<Listener<K>> } = {};

  constructor(opts: ClientOptions = {}) {
    this.baseUrl = (opts.baseUrl ?? defaultBaseUrl()).replace(/\/$/, "");
    this.wsUrl = opts.wsUrl ?? this.baseUrl.replace(/^http/, "ws") + "/ws";
    this.reconnectEnabled = opts.reconnect !== false;
    this.wsImpl = opts.webSocketImpl ?? WebSocket;
  }

  // ---------------------------------------------------------------- 事件订阅
  on<K extends keyof EventMap>(kind: K, cb: Listener<K>): () => void {
    let set = this.listeners[kind] as Set<Listener<K>> | undefined;
    if (!set) {
      set = new Set();
      (this.listeners as Record<string, unknown>)[kind] = set;
    }
    set.add(cb);
    return () => set!.delete(cb);
  }

  private emit<K extends keyof EventMap>(kind: K, payload: EventMap[K]): void {
    const set = this.listeners[kind] as Set<Listener<K>> | undefined;
    if (!set) return;
    for (const cb of [...set]) {
      try {
        cb(payload);
      } catch (err) {
        console.error(`[assistant-client] listener ${kind} 抛错`, err);
      }
    }
  }

  // ---------------------------------------------------------------- REST
  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const res = await fetch(this.baseUrl + path, {
      headers: { "Content-Type": "application/json" },
      ...init,
    });
    if (!res.ok && res.status >= 500) {
      throw new Error(`adapter ${res.status}: ${await res.text()}`);
    }
    return (await res.json()) as T;
  }

  private async get<T>(path: string, params?: Record<string, string | number | boolean | undefined | null>): Promise<T> {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params ?? {})) {
      if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
    }
    const suffix = qs.toString();
    return this.request<T>(path + (suffix ? `?${suffix}` : ""));
  }

  status(): Promise<StatusSnapshot> {
    return this.get("/api/status");
  }

  sessions(opts: { status?: string; limit?: number; offset?: number } = {}): Promise<SessionsPage> {
    return this.get("/api/sessions", opts);
  }

  session(id: string, opts: PageOpts = {}): Promise<SessionDetail> {
    return this.get(`/api/sessions/${encodeURIComponent(id)}`, {
      cursor: opts.cursor, limit: opts.limit, include_tools: opts.includeTools,
    });
  }

  sessionLog(id: string, bytes = 65536): Promise<LogResponse> {
    return this.get(`/api/sessions/${encodeURIComponent(id)}/log`, { bytes });
  }

  routerContext(opts: PageOpts = {}): Promise<ContextPage> {
    return this.get("/api/router/context", {
      cursor: opts.cursor, limit: opts.limit, include_tools: opts.includeTools,
    });
  }

  turns(limit = 50): Promise<ListResponse<TurnRecord>> {
    return this.get("/api/turns", { limit });
  }

  inbox(limit = 100): Promise<ListResponse<InboxRecord>> {
    return this.get("/api/inbox", { limit });
  }

  schedules(): Promise<ListResponse<ScheduleItem>> {
    return this.get("/api/schedules");
  }

  watches(): Promise<ListResponse<WatchItem>> {
    return this.get("/api/watches");
  }

  costs(limit = 100): Promise<CostsResponse> {
    return this.get("/api/costs", { limit });
  }

  events(stream: "router" | "outbox" = "router", count = 50): Promise<EventsResponse> {
    return this.get("/api/events", { stream, count });
  }

  // ---------------------------------------------------------------- WebSocket
  get connected(): boolean {
    return this.ws !== null && this.ws.readyState === this.wsImpl.OPEN;
  }

  connect(): void {
    if (this.disposed || this.ws) return;
    let ws: WebSocket;
    try {
      ws = new this.wsImpl(this.wsUrl);
    } catch (err) {
      this.emit("error", err instanceof Error ? err : new Error(String(err)));
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.backoffMs = BACKOFF_MIN_MS;
      this.emit("open", undefined);
    };
    ws.onmessage = (msg: MessageEvent) => {
      let frame: ServerFrame;
      try {
        frame = JSON.parse(String(msg.data)) as ServerFrame;
      } catch {
        return;
      }
      this.handleFrame(frame);
    };
    ws.onerror = () => {
      this.emit("error", new Error("websocket error"));
    };
    ws.onclose = (ev: CloseEvent) => {
      this.ws = null;
      this.emit("close", { code: ev.code, reason: ev.reason });
      this.scheduleReconnect();
    };
  }

  private handleFrame(frame: ServerFrame): void {
    switch (frame.type) {
      case "hello":
        for (const e of frame.events) this.markSeen(e);
        this.emit("hello", { status: frame.status, events: frame.events });
        break;
      case "event": {
        const e: StreamEvent = { stream: frame.stream, entry_id: frame.entry_id, event: frame.event };
        if (this.isSeen(e)) return;   // 重连回放去重
        this.markSeen(e);
        this.emit("event", e);
        break;
      }
      case "status":
        this.emit("status", frame.status);
        break;
      case "pong":
        this.emit("pong", undefined);
        break;
    }
  }

  private eventKey(e: StreamEvent): string {
    return `${e.stream}:${e.entry_id}`;
  }

  private isSeen(e: StreamEvent): boolean {
    return this.seen.has(this.eventKey(e));
  }

  private markSeen(e: StreamEvent): void {
    const key = this.eventKey(e);
    if (this.seen.has(key)) return;
    this.seen.add(key);
    this.seenOrder.push(key);
    if (this.seenOrder.length > REPLAY_DEDUPE_MAX) {
      const old = this.seenOrder.shift();
      if (old) this.seen.delete(old);
    }
  }

  private scheduleReconnect(): void {
    if (this.disposed || !this.reconnectEnabled || this.reconnectTimer) return;
    const delay = this.backoffMs;
    this.backoffMs = Math.min(this.backoffMs * 2, BACKOFF_MAX_MS);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  ping(): void {
    if (this.connected) this.ws!.send(JSON.stringify({ type: "ping" } satisfies ClientFrame));
  }

  disconnect(): void {
    this.disposed = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
  }
}