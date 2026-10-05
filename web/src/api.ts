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
export const login = (password: string, email?: string) =>
  api<Me>("/api/auth/login", { method: "POST", body: JSON.stringify({ password, ...(email ? { email } : {}) }) });
export const acceptInvite = (token: string, password: string) =>
  api<Me>("/api/auth/accept", { method: "POST", body: JSON.stringify({ token, password }) });
export const logout = () => api<Me>("/api/auth/logout", { method: "POST" });
