import { useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import type { StatusSnapshot, StreamEvent } from "@personal-assistant/client";
import { client, extractFileRefs, fileName, fileKind, fileUrl, fmtTime, userText } from "./lib.js";
import FilePreview from "./FilePreview.js";

interface ChatMsg { key: string; role: "user" | "assistant"; text: string; ts: string; via: string; files: string[] }

interface ChatComment { id: string; quote: string; text: string; ts: number }
const COMMENT_STORAGE_KEY = "personal-agent-comments-v1";

function loadComments(): Record<string, ChatComment[]> {
  try {
    const raw = localStorage.getItem(COMMENT_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, ChatComment[]>;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

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
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState("");
  const [sendInfo, setSendInfo] = useState("");
  const [preview, setPreview] = useState<string | null>(null);
  const [previewRatio, setPreviewRatio] = useState(0.5);
  const [comments, setComments] = useState<Record<string, ChatComment[]>>(loadComments);
  const [selection, setSelection] = useState<{ x: number; y: number; left: number; top: number; bottom: number; key: string; quote: string } | null>(null);
  const [commentDraft, setCommentDraft] = useState<{ key: string; quote: string; left: number; top: number; bottom: number } | null>(null);
  const [commentText, setCommentText] = useState("");
  const [commentSending, setCommentSending] = useState(false);
  const chatBodyRef = useRef<HTMLDivElement>(null);
  const chatId = status?.router.state.last_chat_id ?? "";
  const gatewayOffline = status?.gateway.alive === false;

  useEffect(() => {
    try {
      localStorage.setItem(COMMENT_STORAGE_KEY, JSON.stringify(comments));
    } catch {
      // localStorage 不可用时只保留当前会话内评论
    }
  }, [comments]);

  function handleSelection() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) {
      setSelection(null);
      return;
    }
    const quote = sel.toString().trim();
    if (!quote) return;
    const anchor = sel.anchorNode;
    const element = anchor?.nodeType === Node.ELEMENT_NODE
      ? (anchor as Element)
      : anchor?.parentElement;
    const bubble = element?.closest(".bubble");
    const message = bubble?.closest("[data-msg-key]");
    const key = message?.getAttribute("data-msg-key");
    if (!bubble || !key) {
      setSelection(null);
      return;
    }
    const rect = sel.getRangeAt(0).getBoundingClientRect();
    setSelection({
      x: Math.min(Math.max(rect.left + rect.width / 2, 70), window.innerWidth - 70),
      y: Math.min(rect.bottom + 8, window.innerHeight - 48),
      left: rect.left,
      top: rect.top,
      bottom: rect.bottom,
      key,
      quote: quote.slice(0, 1200),
    });
  }

  function startResize(e: ReactMouseEvent<HTMLDivElement>) {
    e.preventDefault();
    const parent = e.currentTarget.parentElement;
    if (!parent) return;
    const startX = e.clientX;
    const startRatio = previewRatio;
    const width = parent.getBoundingClientRect().width;
    document.body.classList.add("is-resizing");
    const onMove = (ev: MouseEvent) => {
      const next = startRatio + (ev.clientX - startX) / width;
      setPreviewRatio(Math.min(0.7, Math.max(0.3, next)));
    };
    const onUp = () => {
      document.body.classList.remove("is-resizing");
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  function removeComment(key: string, id: string) {
    setComments((prev) => {
      const next = { ...prev, [key]: (prev[key] ?? []).filter((c) => c.id !== id) };
      if (!next[key].length) delete next[key];
      return next;
    });
  }

  function saveComment(sendToAssistant: boolean) {
    if (!commentDraft || !commentText.trim() || commentSending) return;
    const item: ChatComment = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      quote: commentDraft.quote,
      text: commentText.trim(),
      ts: Date.now(),
    };
    setComments((prev) => ({ ...prev, [commentDraft.key]: [...(prev[commentDraft.key] ?? []), item] }));
    const commentTextToSend = commentText.trim();
    const quoteToSend = commentDraft.quote;
    setCommentDraft(null);
    setCommentText("");
    if (!sendToAssistant) return;
    if (gatewayOffline) {
      setSendError("gateway 离线，评论已保存但未发送给助理");
      return;
    }
    setCommentSending(true);
    const quoted = quoteToSend.split("\n").map((line) => `> ${line}`).join("\n");
    client.sendMessage(`【评论】\n${quoted}\n\n${commentTextToSend}`)
      .then((res) => {
        if (!res.ok) setSendError(res.message || res.error || "评论发送失败");
        else if (res.echo && !res.echo.ok) setSendError(`评论已保存，但手机飞书同步失败：${res.echo.error ?? "未知错误"}`);
        else setSendInfo("评论已保存并发送给助理");
      })
      .catch((err) => setSendError(err instanceof Error ? err.message : String(err)))
      .finally(() => setCommentSending(false));
  }

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

  useEffect(() => {
    // 只滚聊天面板内部，避免 scrollIntoView 连带滚动外层容器
    const el = chatBodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [chat.length]);

  return (
    <div className="overview">
      <div
        className={`overview-row ${preview ? "with-preview" : ""}`}
        style={preview ? { gridTemplateColumns: `minmax(0, ${1 - previewRatio}fr) 8px minmax(0, ${previewRatio}fr)` } : undefined}
      >
        <div className="chat-panel">
          <div className="panel-box">
            <div className="panel-head">
              对话（网页 ↔ 飞书）
              <span className="badge green">可发送</span>
              <span className="spacer" />
              <span className="dim small mono">{chatId ? `chat ${chatId.slice(-8)}` : "未绑定会话"}</span>
            </div>
            <div
              className="panel-body"
              ref={chatBodyRef}
              onMouseUp={handleSelection}
              onScroll={() => setSelection(null)}
            >
              {chat.length === 0 && <div className="empty">暂无对话记录</div>}
              {chat.map((m) => (
                <div className={`chat-msg ${m.role}`} key={m.key} data-msg-key={m.key}>
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
                  {(comments[m.key] ?? []).length > 0 && (
                    <div className="comment-list">
                      {comments[m.key].map((c) => (
                        <div className="comment-item" key={c.id}>
                          <blockquote>{c.quote}</blockquote>
                          <div className="comment-text">{c.text}</div>
                          <button type="button" className="comment-del" onClick={() => removeComment(m.key, c.id)}>删除</button>
                        </div>
                      ))}
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
        {preview && (
          <>
            <div className="split-handle" role="separator" aria-orientation="vertical" onMouseDown={startResize} title="左右拖动调整宽度" />
            <FilePreview path={preview} onClose={() => setPreview(null)} />
          </>
        )}
      </div>
      {selection && (
        <button
          type="button"
          className="selection-comment-btn"
          style={{ left: selection.x, top: selection.y }}
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => {
            setCommentDraft({
              key: selection.key,
              quote: selection.quote,
              left: selection.left,
              top: selection.top,
              bottom: selection.bottom,
            });
            setCommentText("");
            setSelection(null);
            window.getSelection()?.removeAllRanges();
          }}
        >
          评论选中文本
        </button>
      )}
      {commentDraft && (
        <div
          className="comment-editor"
          style={{
            left: Math.max(8, Math.min(commentDraft.left, window.innerWidth - 380)),
            top: commentDraft.bottom + 248 < window.innerHeight
              ? commentDraft.bottom + 8
              : Math.max(8, commentDraft.top - 248),
          }}
        >
          <div className="comment-editor-head">评论选中文本</div>
          <blockquote>{commentDraft.quote}</blockquote>
          <textarea
            autoFocus
            rows={3}
            value={commentText}
            placeholder="写下你的评论…"
            onChange={(e) => setCommentText(e.target.value)}
          />
          <div className="comment-editor-actions">
            <button
              type="button"
              className="btn"
              onClick={() => {
                setCommentDraft(null);
                setCommentText("");
              }}
            >
              取消
            </button>
            <button
              type="button"
              className="btn"
              disabled={!commentText.trim() || commentSending}
              onClick={() => saveComment(false)}
            >
              保存
            </button>
            <button
              type="button"
              className="btn primary"
              disabled={!commentText.trim() || commentSending}
              onClick={() => saveComment(true)}
            >
              {commentSending ? "发送中…" : "保存并发给助理"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}