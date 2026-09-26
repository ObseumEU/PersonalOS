import ReactMarkdown, { type Components, defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Task text, notes and answers as formatted Markdown (GitHub flavour: tables,
 * task lists, strikethrough, autolinks). react-markdown builds React elements
 * and never renders raw HTML (`skipHtml`), so a note cannot inject markup or
 * scripts; links other than http(s), mailto and in-app paths are dropped.
 *
 * `compact` is for comments, progress and other short text in a list.
 */
export default function Markdown({ text, compact = false, className = "" }: { text: string; compact?: boolean; className?: string }) {
  return (
    <div className={`md ${compact ? "md-compact" : ""} ${className}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml urlTransform={safeUrl} components={components}>
        {clean(text)}
      </ReactMarkdown>
    </div>
  );
}

/** Wrapped outside content (<external …>) shows as its text, not as tags. */
function clean(text: string): string {
  return (text ?? "").replace(/\r\n/g, "\n").replace(/<\/?external\b[^>]*>/g, "");
}

function safeUrl(url: string): string {
  const u = defaultUrlTransform(url);
  return /^(https?:|mailto:|\/(?!\/)|#)/i.test(u) ? u : "";
}

const components: Components = {
  a: ({ href, children }) =>
    href ? (
      <a
        href={href}
        target={href.startsWith("/") || href.startsWith("#") ? undefined : "_blank"}
        rel="noreferrer noopener"
      >
        {children}
      </a>
    ) : (
      <span>{children}</span>
    ),
  // Images from task text could track the reader; show them as links instead.
  img: ({ src, alt }) =>
    typeof src === "string" && src ? (
      <a href={src} target="_blank" rel="noreferrer noopener">
        {alt || src}
      </a>
    ) : null,
  table: ({ children }) => (
    <div className="md-table">
      <table>{children}</table>
    </div>
  ),
};
