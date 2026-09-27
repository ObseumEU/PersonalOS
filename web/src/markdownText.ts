import { stripToolMarkup } from "./toolMarkup";

/**
 * Markdown as a one-line plain-text snippet, for previews in lists and chips
 * (the full text renders with components/Markdown). Drops code blocks, HTML
 * and wrapper tags, keeps link and image labels, flattens tables and lists.
 */
export function markdownSnippet(text: string | null | undefined, max = 180): string {
  const out = stripToolMarkup(text)
    .replace(/<\/?external[^>]*>/g, " ")
    .replace(/```[\s\S]*?(```|$)/g, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/gm, " ")
    .replace(/^\s*([-*_]\s*){3,}$/gm, " ")
    .replace(/\|/g, " · ")
    .replace(/^\s*(#{1,6}|>|[-+*]|\d+[.)])\s+/gm, "")
    .replace(/^\s*\[[ xX]\]\s+/gm, "")
    .replace(/(\*\*|__|~~|`)/g, "")
    .replace(/(^|[\s(])[*_]([^*_\n]+)[*_](?=[\s).,!?:;]|$)/gm, "$1$2")
    .replace(/(\s*·\s*)+/g, " · ")
    .replace(/\s+/g, " ")
    .replace(/^\s*·\s*|\s*·\s*$/g, "")
    .trim();
  return out.length > max ? `${out.slice(0, max - 1).trimEnd()}…` : out;
}
