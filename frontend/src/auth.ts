import { API_URL } from "./api";

export type AuthUser = { id: string; email: string };
export type AuthSession = { token: string; expires_at: string; user: AuthUser };

export type SeriesSelection = { selected: string[]; known: string[] };
export type UserSettings = {
  series_selection: SeriesSelection;
  preview_preferences: Record<string, boolean>;
  seen_episode_ids: string[];
};
export type SettingsSnapshot = {
  data: UserSettings;
  version: number;
  updated_at: string | null;
};

const SESSION_KEY = "episode-calendar-auth-session";

export class ApiError extends Error {
  status: number;
  payload: unknown;

  constructor(status: number, payload: unknown) {
    super(`API request failed with status ${status}`);
    this.status = status;
    this.payload = payload;
  }
}

export function emptySettings(): UserSettings {
  return {
    series_selection: { selected: [], known: [] },
    preview_preferences: {},
    seen_episode_ids: [],
  };
}

export function readSession(): AuthSession | null {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as AuthSession;
    if (!parsed?.token || !parsed.user?.email) return null;
    if (Date.parse(parsed.expires_at) <= Date.now()) {
      localStorage.removeItem(SESSION_KEY);
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

export function writeSession(session: AuthSession) {
  try {
    localStorage.setItem(SESSION_KEY, JSON.stringify(session));
  } catch {
    // Signing in still works for the current page when storage is unavailable.
  }
}

export function clearSession() {
  try {
    localStorage.removeItem(SESSION_KEY);
  } catch {
    // Nothing to clean up when storage is unavailable.
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  token?: string,
): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
  });
  const payload = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(response.status, payload);
  return payload as unknown as T;
}

export async function startEmailLogin(email: string): Promise<void> {
  await request<{ status: string }>("/api/v1/auth/email/start", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}

export async function verifyEmailCode(email: string, code: string): Promise<AuthSession> {
  return request<AuthSession>("/api/v1/auth/email/verify", {
    method: "POST",
    body: JSON.stringify({ email, code }),
  });
}

export async function verifyEmailLink(token: string): Promise<AuthSession> {
  return request<AuthSession>("/api/v1/auth/link/verify", {
    method: "POST",
    body: JSON.stringify({ token }),
  });
}

export async function fetchSettings(session: AuthSession): Promise<SettingsSnapshot> {
  return request<SettingsSnapshot>("/api/v1/me/settings", {}, session.token);
}

export async function saveSettings(
  session: AuthSession,
  data: UserSettings,
  version: number,
): Promise<SettingsSnapshot> {
  return request<SettingsSnapshot>(
    "/api/v1/me/settings",
    { method: "PUT", body: JSON.stringify({ data, version }) },
    session.token,
  );
}

export async function logout(session: AuthSession): Promise<void> {
  await request<void>("/api/v1/auth/logout", { method: "POST" }, session.token);
}

export async function deleteAccount(session: AuthSession): Promise<void> {
  await request<void>("/api/v1/me", { method: "DELETE" }, session.token);
}
