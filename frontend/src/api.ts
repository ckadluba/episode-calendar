export const API_URL =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ??
  "http://localhost:8000";

let activeRequests = 0;
const listeners = new Set<() => void>();

function notifyListeners() {
  listeners.forEach((listener) => listener());
}

export function subscribeApiActivity(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getApiActivitySnapshot() {
  return activeRequests > 0;
}

/** Tracks all in-flight API requests so the UI can show a single shared spinner. */
export async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  activeRequests += 1;
  notifyListeners();
  try {
    return init === undefined ? await fetch(input) : await fetch(input, init);
  } finally {
    activeRequests -= 1;
    notifyListeners();
  }
}
