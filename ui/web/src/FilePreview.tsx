import { useEffect, useMemo, useState } from "react";
import DOMPurify from "dompurify";
import { marked } from "marked";
import { fileKind, fileName, fileUrl } from "./lib.js";

function formatBytes(n: number): string {
  if (!n) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

/** 右侧分栏文件预览：类似 IDE 的编辑器面板，含 tab、路径、行号和源码/渲染切换。 */
export default function FilePreview({ path, onClose }: { path: string; onClose: () => void }) {
  const kind = fileKind(path);
  const name = fileName(path);
  const src = fileUrl(path);
  const ext = name.includes(".") ? name.slice(name.lastIndexOf(".") + 1).toLowerCase() : "";
  const isMarkdown = ext === "md" || ext === "markdown";
  const isHtml = ext === "html" || ext === "htm";
  const canRender = isMarkdown || isHtml;
  const [mode, setMode] = useState<"rendered" | "source">(canRender ? "rendered" : "source");
  const [wrap, setWrap] = useState(true);
  const [text, setText] = useState("");
  const [size, setSize] = useState(0);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(kind === "text");

  useEffect(() => {
    setMode(canRender ? "rendered" : "source");
  }, [canRender, path]);

  useEffect(() => {
    if (kind !== "text") return;
    let alive = true;
    setLoading(true);
    setError("");
    setText("");
    setSize(0);
    fetch(src)
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const n = Number(res.headers.get("content-length") || 0);
        if (n > 1_500_000) throw new Error("文件超过 1.5 MB，请下载后查看");
        if (alive) setSize(n);
        return res.text();
      })
      .then((body) => {
        if (alive) setText(body);
      })
      .catch((err) => {
        if (alive) setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [kind, src]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const rendered = useMemo(() => {
    if (!text) return "";
    if (isMarkdown) return DOMPurify.sanitize(marked.parse(text, { async: false }) as string);
    if (isHtml) return DOMPurify.sanitize(text);
    return "";
  }, [text, isMarkdown, isHtml]);

  const lines = useMemo(() => text.split("\n").slice(0, 5000), [text]);
  const lineTruncated = text.split("\n").length > 5000;
  const badge = kind === "image" ? "IMG" : kind === "pdf" ? "PDF" : kind === "video" ? "VID" : kind === "audio" ? "AUD" : isMarkdown ? "MD" : isHtml ? "HTML" : (ext || "TXT").toUpperCase().slice(0, 4);

  return (
    <aside className="file-panel" role="dialog" aria-label={`文件预览：${name}`}>
      <div className="file-panel-tabs">
        <div className="file-tab active">
          <span className="file-tab-icon">{badge}</span>
          <span className="file-tab-name" title={name}>{name}</span>
          <button type="button" className="file-tab-close" onClick={onClose} title="关闭">×</button>
        </div>
        <span className="spacer" />
        <a className="file-icon-btn" href={fileUrl(path, true)} title="下载">↓</a>
      </div>

      <div className="file-panel-toolbar">
        <span className="mono file-panel-path" title={path}>{path}</span>
        <span className="spacer" />
        {canRender && (
          <div className="file-segment">
            <button type="button" className={mode === "rendered" ? "active" : ""} onClick={() => setMode("rendered")}>预览</button>
            <button type="button" className={mode === "source" ? "active" : ""} onClick={() => setMode("source")}>源码</button>
          </div>
        )}
        {kind === "text" && (
          <button type="button" className="file-tool-btn" onClick={() => setWrap((v) => !v)}>
            {wrap ? "不换行" : "自动换行"}
          </button>
        )}
      </div>

      <div className={`file-panel-body ${kind}`}>
        {kind === "image" && <img src={src} alt={name} />}
        {kind === "pdf" && <iframe src={src} title={name} />}
        {kind === "video" && <video src={src} controls />}
        {kind === "audio" && <audio src={src} controls />}
        {kind === "other" && (
          <div className="file-panel-empty">
            <div className="file-big-icon">{badge}</div>
            <p>此类型不支持在线预览</p>
            <a className="btn primary" href={fileUrl(path, true)}>下载文件</a>
          </div>
        )}
        {kind === "text" &&
          (loading ? (
            <div className="empty">加载中…</div>
          ) : error ? (
            <div className="empty">{error}</div>
          ) : mode === "rendered" && isMarkdown ? (
            <div className="file-markdown" dangerouslySetInnerHTML={{ __html: rendered }} />
          ) : mode === "rendered" && isHtml ? (
            <iframe className="file-html" title={name} sandbox="" srcDoc={rendered} />
          ) : (
            <div className={`code-view ${wrap ? "wrap" : ""}`}>
              {lines.map((line, i) => (
                <div className="code-line" key={i}>
                  <span className="code-ln">{i + 1}</span>
                  <span className="code-text">{line || "\u00a0"}</span>
                </div>
              ))}
              {lineTruncated && <div className="code-truncated">仅显示前 5000 行，完整内容请下载</div>}
            </div>
          ))}
      </div>

      <div className="file-panel-status">
        <span>{badge}{size ? ` · ${formatBytes(size)}` : ""}{kind === "text" ? ` · ${lines.length}${lineTruncated ? "+" : ""} 行` : ""}</span>
        <span className="spacer" />
        <span>{mode === "rendered" && canRender ? "渲染视图" : "源码视图"}</span>
      </div>
    </aside>
  );
}
