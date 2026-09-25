export type Me = { authenticated: boolean; login_required: boolean };

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body.detail ?? res.statusText);
  }
  return res.json() as Promise<T>;
}

export const getMe = () => api<Me>("/api/auth/me");
export const login = (password: string) =>
  api<Me>("/api/auth/login", { method: "POST", body: JSON.stringify({ password }) });
export const logout = () => api<Me>("/api/auth/logout", { method: "POST" });
