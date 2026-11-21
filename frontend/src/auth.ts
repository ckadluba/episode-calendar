import { API_URL } from "./api";
import type {
  CreationOptionsJSON,
  RequestOptionsJSON,
  WebAuthnCredentialJSON,
} from "./webauthn";

export type AuthUser = { id: string };
export type AuthSession = { token: string; expires_at: string; user: AuthUser };
export type AuthSessionWithRecovery = AuthSession & { recovery_code: string };
export type PasskeyInfo = {
  credential_id: string;
  created_at: string;
  last_used_at: string | null;
};

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
    if (!parsed?.token || !parsed.user?.id) return null;
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

async function request<T>(path: string, init: RequestInit = {}, token?: string): Promise<T> {
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

export function registrationOptions(): Promise<CreationOptionsJSON> {
  return request<CreationOptionsJSON>("/api/v1/auth/registration/options", { method: "POST" });
}

export function completeRegistration(
  challenge: string,
  credential: WebAuthnCredentialJSON,
): Promise<AuthSessionWithRecovery> {
  return request<AuthSessionWithRecovery>("/api/v1/auth/registration/verify", {
    method: "POST",
    body: JSON.stringify({ challenge, credential }),
  });
}

export function authenticationOptions(): Promise<RequestOptionsJSON> {
  return request<RequestOptionsJSON>("/api/v1/auth/authentication/options", { method: "POST" });
}

export function completeAuthentication(
  challenge: string,
  credential: WebAuthnCredentialJSON,
): Promise<AuthSession> {
  return request<AuthSession>("/api/v1/auth/authentication/verify", {
    method: "POST",
    body: JSON.stringify({ challenge, credential }),
  });
}

export function passkeyAdditionOptions(session: AuthSession): Promise<CreationOptionsJSON> {
  return request<CreationOptionsJSON>("/api/v1/auth/passkeys/options", { method: "POST" }, session.token);
}

export function addPasskey(
  session: AuthSession,
  challenge: string,
  credential: WebAuthnCredentialJSON,
): Promise<PasskeyInfo> {
  return request<PasskeyInfo>(
    "/api/v1/auth/passkeys",
    { method: "POST", body: JSON.stringify({ challenge, credential }) },
    session.token,
  );
}

export function listPasskeys(session: AuthSession): Promise<PasskeyInfo[]> {
  return request<PasskeyInfo[]>("/api/v1/auth/passkeys", {}, session.token);
}

export function deletePasskey(session: AuthSession, credentialId: string): Promise<void> {
  return request<void>(
    `/api/v1/auth/passkeys/${encodeURIComponent(credentialId)}`,
    { method: "DELETE" },
    session.token,
  );
}

export function recoverAccount(code: string): Promise<AuthSessionWithRecovery> {
  return request<AuthSessionWithRecovery>("/api/v1/auth/recovery/verify", {
    method: "POST",
    body: JSON.stringify({ code }),
  });
}

export function rotateRecoveryCode(session: AuthSession): Promise<{ recovery_code: string }> {
  return request<{ recovery_code: string }>(
    "/api/v1/auth/recovery/rotate",
    { method: "POST" },
    session.token,
  );
}

export function fetchSettings(session: AuthSession): Promise<SettingsSnapshot> {
  return request<SettingsSnapshot>("/api/v1/me/settings", {}, session.token);
}

export function saveSettings(
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

export function logout(session: AuthSession): Promise<void> {
  return request<void>("/api/v1/auth/logout", { method: "POST" }, session.token);
}

export function deleteAccount(session: AuthSession): Promise<void> {
  return request<void>("/api/v1/me", { method: "DELETE" }, session.token);
}
