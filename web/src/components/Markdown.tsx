import ReactMarkdown, { type Components, defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Link } from "react-router-dom";
import { linkRefsMarkdown, parseRefHref } from "../refs";
import { TaskLink } from "../taskSheet";
import { ToolChip, toolMarkupToMarkdown } from "../toolMarkup";
import { RefChip } from "./RefPreview";

/**
 * Task text, notes and answers as formatted Markdown (GitHub flavour: tables,
 * task lists, strikethrough, autolinks). react-markdown builds React elements
 * and never renders raw HTML (`skipHtml`), so a note cannot inject markup or
 * scripts; links other than http(s), mailto and in-app paths are dropped.
 *
 * `compact` is for comments, progress and other short text in a list.
 * In-app links stay in the app; a task reference (T-123) opens the task panel; notes, chat messages,
 * knowledge-base passages and files an agent mentions ("poznámka 23", "msg 1095", `k834E6OTGTM:c17`)
 * become chips that open a preview in place (refs.ts, RefPreview.tsx).
 */
export default function Markdown({ text, compact = false, className = "" }: { text: string; compact?: boolean; className?: string }) {
  return (
    <div className={`md ${compact ? "md-compact" : ""} ${className}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml urlTransform={safeUrl} components={components}>
        {linkRefs(linkRefsMarkdown(clean(text)))}
      </ReactMarkdown>
    </div>
  );
}

/** T-123 outside code and links becomes a link to the task. */
function linkRefs(text: string): string {
  return text
    .split(/(```[\s\S]*?(?:```|$)|`[^`\n]*`)/)
    .map((part, i) => (i % 2 ? part : part.replace(/(^|[^\w[/#=-])(T-\d{1,6})\b(?![\]\w(])/g, "$1[$2](/tasks/$2)")))
    .join("");
}

/** Wrapped outside content (<external …>) shows as its text, not as tags. */
function clean(text: string): string {
  // Tool calls a model wrote as text become a muted chip (toolMarkup, the `a` component below).
  return toolMarkupToMarkdown((text ?? "").replace(/\r\n/g, "\n").replace(/<\/?external\b[^>]*>/g, ""));
}

function safeUrl(url: string): string {
  const u = defaultUrlTransform(url);
  return /^(https?:|mailto:|\/(?!\/)|#)/i.test(u) ? u : "";
}

const components: Components = {
  a: ({ href, children }) => {
    if (href === "#pos-tool") return <ToolChip label={String(Array.isArray(children) ? children.join("") : children ?? "")} />;
    const pref = parseRefHref(href);
    if (pref) return <RefChip kind={pref.kind} id={pref.id} label={String(Array.isArray(children) ? children.join("") : children ?? "")} />;
    const ref = href?.match(/^\/tasks(?:\/|\?(?:.*&)?task=)(T-\d+)/i);
    if (ref) {
      const label = String(Array.isArray(children) ? children.join("") : children ?? "");
      // A bare "T-431" shows the task's title too; a link with its own words stays as written.
      if (label.toUpperCase() === ref[1].toUpperCase()) return <RefChip kind="task" id={ref[1].toUpperCase()} />;
      return <TaskLink taskRef={ref[1].toUpperCase()}>{children}</TaskLink>;
    }
    if (href && href.startsWith("/") && !href.startsWith("/api/")) return <Link to={href}>{children}</Link>;
    return href ? (
      <a
        href={href}
        target={href.startsWith("/") || href.startsWith("#") ? undefined : "_blank"}
        rel="noreferrer noopener"
      >
        {children}
      </a>
    ) : (
      <span>{children}</span>
    );
  },
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
