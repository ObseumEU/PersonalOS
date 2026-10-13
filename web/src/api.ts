export type Me = {
  authenticated: boolean;
  login_required: boolean;
  actor_id?: number | null;
  name?: string | null;
  is_owner?: boolean;
};

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** A read that takes longer than this ends in an error with "Zkusit znovu", never an endless "načítám…". */
export const READ_TIMEOUT_MS = 15_000;
/** Reads that generate something first (an LLM summary or report: ?generate=true) get longer. */
export const GENERATE_TIMEOUT_MS = 90_000;
/** Writes (POST, PATCH, …): the edge proxy gives up at 150 s; the screen says so a little before. */
export const WRITE_TIMEOUT_MS = 120_000;

export type ApiInit = RequestInit & {
  /** Milliseconds before the request is given up (0 = never); default by method, see timeoutFor. */
  timeoutMs?: number;
};

/** How long a request may take before it ends in an error. */
export function timeoutFor(path: string, init?: ApiInit): number {
  if (init?.timeoutMs !== undefined) return init.timeoutMs;
  const method = (init?.method ?? "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD") return WRITE_TIMEOUT_MS;
  return /[?&]generate=true\b/.test(path) ? GENERATE_TIMEOUT_MS : READ_TIMEOUT_MS;
}

/** The owner-facing words for a request that failed before any answer (status 0). */
export const timeoutMessage = (ms: number) => `Server neodpověděl do ${Math.round(ms / 1000)} s.`;
export const NETWORK_MESSAGE = "Nelze se spojit se serverem.";

/**
 * fetch with a deadline: past `ms` the request is aborted and rejects with ApiError(0, "Server neodpověděl…").
 * A caller's own signal still aborts it as before (AbortError). A network failure is ApiError(0, …) too,
 * so every screen can show one message and "Zkusit znovu".
 */
export async function fetchWithTimeout(
  input: string,
  init: RequestInit,
  ms: number,
  read: (res: Response) => Promise<unknown> = async () => undefined,
  doFetch: typeof fetch = fetch,
): Promise<{ res: Response; body: unknown }> {
  const ctl = new AbortController();
  let timedOut = false;
  const outer = init.signal;
  const onOuter = () => ctl.abort(outer?.reason);
  if (outer) {
    if (outer.aborted) ctl.abort(outer.reason);
    else outer.addEventListener("abort", onOuter, { once: true });
  }
  const timer = ms > 0 ? setTimeout(() => ((timedOut = true), ctl.abort()), ms) : null;
  try {
    const res = await doFetch(input, { ...init, signal: ctl.signal });
    const body = await read(res);
    return { res, body };
  } catch (e) {
    if (timedOut) throw new ApiError(0, timeoutMessage(ms));
    if (outer?.aborted) throw e;
    if (e instanceof ApiError) throw e;
    if (e instanceof TypeError) throw new ApiError(0, NETWORK_MESSAGE);
    throw e;
  } finally {
    if (timer) clearTimeout(timer);
    outer?.removeEventListener("abort", onOuter);
  }
}

export async function api<T>(path: string, init?: ApiInit): Promise<T> {
  const { timeoutMs: _ignored, ...rest } = init ?? {};
  const { body } = await fetchWithTimeout(
    path,
    { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...rest },
    timeoutFor(path, init),
    async (res) => {
      if (!res.ok) {
        const b = await res.json().catch(() => ({}));
        throw new ApiError(res.status, b.detail ?? res.statusText);
      }
      return res.json();
    },
  );
  return body as T;
}

export const getMe = () => api<Me>("/api/auth/me");
export const login = (password: string, email?: string) =>
  api<Me>("/api/auth/login", { method: "POST", body: JSON.stringify({ password, ...(email ? { email } : {}) }) });
export const acceptInvite = (token: string, password: string) =>
  api<Me>("/api/auth/accept", { method: "POST", body: JSON.stringify({ token, password }) });
export const logout = () => api<Me>("/api/auth/logout", { method: "POST" });
