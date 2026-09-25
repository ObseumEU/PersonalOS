import { ApiError, api } from "./api";
import type { Task, Version } from "./tasksApi";
import type { CalEvent } from "./pages/Calendar";

export type Visibility = "public" | "team" | "private";

export type FileItem = {
  id: number;
  name: string;
  mime: string | null;
  size: number;
  sha256: string;
  topic: string | null;
  tags: string[];
  visibility: Visibility;
  owner_id: number;
  created_by: number | null;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
  text_chars: number;
  preview: "image" | "pdf" | "text" | "download";
  duplicate?: boolean;
  /** The file's document in knowlage, where its text is indexed and searched. */
  kb_doc_id?: string | null;
  kb_status?: "pending" | "ok" | "error" | null;
  kb_error?: string | null;
};

/** "knowlage": full text through knowlage; "filename": knowlage was unreachable, only names matched. */
export type FileSearch = { mode: "knowlage" | "filename" | "none"; files: FileItem[]; error?: string };

export type Note = {
  id: number;
  title: string;
  body?: string;
  excerpt?: string;
  topic: string | null;
  tags: string[];
  visibility: Visibility;
  owner_id: number;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
};

export type TopicCounts = { open_tasks: number; done_tasks: number; upcoming: number; file_count: number; note_count: number };

export type Topic = TopicCounts & {
  id: number | null;
  slug: string;
  name: string;
  description: string;
  color: string | null;
  updated_at: string | null;
  archived_at: string | null;
};

export type TopicDetail = Topic & { files: FileItem[]; notes: Note[]; open: Task[]; done: Task[]; events: CalEvent[] };

export type SearchResult = { q: string; tasks: Task[]; files: FileItem[]; files_mode?: FileSearch["mode"]; notes: Note[] };

const post = <T,>(path: string, body: unknown = {}) => api<T>(path, { method: "POST", body: JSON.stringify(body) });
const patch = <T,>(path: string, body: unknown) => api<T>(path, { method: "PATCH", body: JSON.stringify(body) });

function query(params: Record<string, string | boolean | undefined | null>) {
  const p = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => v && p.set(k, String(v)));
  const s = p.toString();
  return s ? `?${s}` : "";
}

async function upload(file: File, fields: { topic?: string; tags?: string; visibility?: string } = {}): Promise<FileItem> {
  const form = new FormData();
  form.append("file", file);
  Object.entries(fields).forEach(([k, v]) => v && form.append(k, v));
  // No Content-Type header: the browser sets the multipart boundary.
  const res = await fetch("/api/files", { method: "POST", body: form, credentials: "same-origin" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body.detail ?? res.statusText);
  }
  return res.json();
}

export const filesApi = {
  list: (f: { topic?: string; tag?: string; q?: string; archived?: boolean } = {}) => api<FileItem[]>(`/api/files${query(f)}`),
  search: (f: { q: string; topic?: string; tag?: string; archived?: boolean }) => api<FileSearch>(`/api/files/search${query(f)}`),
  tags: () => api<{ tag: string; n: number }[]>("/api/files/tags"),
  get: (id: number) => api<FileItem>(`/api/files/${id}`),
  upload,
  contentUrl: (id: number, download = false) => `/api/files/${id}/content${download ? "?download=1" : ""}`,
  text: async (id: number) => {
    const res = await fetch(`/api/files/${id}/content`, { credentials: "same-origin" });
    if (!res.ok) throw new ApiError(res.status, res.statusText);
    return res.text();
  },
  update: (id: number, changes: Partial<Pick<FileItem, "name" | "topic" | "tags" | "visibility">>) => patch<FileItem>(`/api/files/${id}`, changes),
  archive: (id: number) => post<FileItem>(`/api/files/${id}/archive`),
  restore: (id: number) => post<FileItem>(`/api/files/${id}/restore`),
  history: (id: number) => api<Version[]>(`/api/files/${id}/history`),
};

export const notesApi = {
  list: (f: { topic?: string; tag?: string; q?: string; archived?: boolean } = {}) => api<Note[]>(`/api/notes${query(f)}`),
  get: (id: number) => api<Note>(`/api/notes/${id}`),
  create: (fields: { title: string; body?: string; topic?: string | null; tags?: string[] }) => post<Note>("/api/notes", fields),
  update: (id: number, changes: Partial<Pick<Note, "title" | "body" | "topic" | "tags" | "visibility">>) => patch<Note>(`/api/notes/${id}`, changes),
  archive: (id: number) => post<Note>(`/api/notes/${id}/archive`),
  unarchive: (id: number) => post<Note>(`/api/notes/${id}/restore`),
  restore: (id: number, version: number) => post<Note>(`/api/notes/${id}/restore`, { version }),
  history: (id: number) => api<Version[]>(`/api/notes/${id}/history`),
};

export const topicsApi = {
  list: () => api<Topic[]>("/api/topics"),
  get: (slug: string) => api<TopicDetail>(`/api/topics/${encodeURIComponent(slug)}`),
  create: (fields: { name: string; description?: string; color?: string }) => post<TopicDetail>("/api/topics", fields),
  update: (slug: string, changes: Partial<Pick<Topic, "name" | "description" | "color">>) =>
    patch<TopicDetail>(`/api/topics/${encodeURIComponent(slug)}`, changes),
  archive: (slug: string) => post<Topic>(`/api/topics/${encodeURIComponent(slug)}/archive`),
};

export const searchApi = (q: string) => api<SearchResult>(`/api/search?q=${encodeURIComponent(q)}`);

export function fmtSize(n: number) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10 * 1024 ? 1 : 0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function fmtDate(iso: string) {
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

export const parseTags = (s: string) =>
  s
    .split(",")
    .map((t) => t.trim().replace(/^#/, "").toLowerCase())
    .filter(Boolean);
