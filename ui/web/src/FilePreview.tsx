import { useEffect, useMemo, useState } from "react";
import DOMPurify from "dompurify";
import { marked } from "marked";
import { fileKind, fileName, fileUrl } from "./lib.js";

/** 文件预览弹层：图片/PDF/音视频用浏览器原生能力，文本 fetch 后按纯文本渲染。 */
export default function FilePreview({ path, onClose }: { path: string; onClose: () => void }) {
  const kind = fileKind(path);
  const name = fileName(path);
  const src = fileUrl(path);
  const ext = name.includes(".") ? name.slice(name.lastIndexOf(".") + 1).toLowerCase() : "";
  const isMarkdown = ext === "md" || ext === "markdown";
  const isHtml = ext === "html" || ext === "htm";
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(kind === "text");

  const rendered = useMemo(() => {
    if (!text) return "";
    if (isMarkdown) {
      return DOMPurify.sanitize(marked.parse(text, { async: false }) as string);
    }
    if (isHtml) {
      return DOMPurify.sanitize(text);
    }
    return "";
  }, [text, isMarkdown, isHtml]);

  useEffect(() => {
    if (kind !== "text") return;
    let alive = true;
    setLoading(true);
    setError("");
    setText("");
    fetch(src)
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const len = Number(res.headers.get("content-length") || 0);
        if (len > 1_500_000) throw new Error("文件超过 1.5 MB，请下载后查看");
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

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="file-modal" onClick={(e) => e.stopPropagation()}>
        <div className="file-modal-head">
          <span className="mono">{name}</span>
          <span className="spacer" />
          <a className="btn" href={fileUrl(path, true)}>下载</a>
          <button className="btn" onClick={onClose}>关闭</button>
        </div>
        <div className="file-modal-body">
          {kind === "image" && <img src={src} alt={name} />}
          {kind === "pdf" && <iframe src={src} title={name} />}
          {kind === "video" && <video src={src} controls />}
          {kind === "audio" && <audio src={src} controls />}
          {kind === "text" &&
            (loading ? (
              <div className="empty">加载中…</div>
            ) : error ? (
              <div className="empty">{error}</div>
            ) : isMarkdown ? (
              <div className="file-markdown" dangerouslySetInnerHTML={{ __html: rendered }} />
            ) : isHtml ? (
              <iframe className="file-html" title={name} sandbox="" srcDoc={rendered} />
            ) : (
              <pre className="file-preview-text">{text}</pre>
            ))}
          {kind === "other" && <div className="empty">此类型不支持在线预览，请下载后查看。</div>}
        </div>
        <div className="file-modal-path mono" title={path}>{path}</div>
      </div>
    </div>
  );
}
