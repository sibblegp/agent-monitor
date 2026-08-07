/**
 * Shell adapter + API client.
 *
 * This is the only file that knows whether we're running inside Electron or a
 * plain browser. Everything else calls `pickDirectory()` and doesn't care.
 */

const params = new URLSearchParams(location.search);
export const TOKEN = params.get('token') || '';

/** True when the Electron preload bridge is present. */
export const isElectron = typeof window.codeviz === 'object' && window.codeviz !== null;

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function request(path, { method = 'GET', body } = {}) {
  const url = new URL(path, location.origin);
  const res = await fetch(url, {
    method,
    headers: {
      'x-codeviz-token': TOKEN,
      ...(body ? { 'content-type': 'application/json' } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail || data.error || detail;
    } catch {
      /* non-JSON error body — keep the status text */
    }
    throw new ApiError(detail, res.status);
  }
  return res.json();
}

export const api = {
  state: () => request('/api/state'),
  snapshot: () => request('/api/snapshot'),
  open: (path, scope) => request('/api/open', { method: 'POST', body: { path, scope } }),
  setMode: (mode, ref) => request('/api/mode', { method: 'POST', body: { mode, ref } }),
  refresh: () => request('/api/refresh', { method: 'POST' }),
  pause: (paused) => request('/api/pause', { method: 'POST', body: { paused } }),
  fs: (path, hidden) =>
    request(`/api/fs?${new URLSearchParams({ ...(path ? { path } : {}), hidden: hidden ? '1' : '' })}`),
  recents: () => request('/api/recents'),
  commits: () => request('/api/commits'),
  branches: () => request('/api/branches'),
  settings: (patch) => request('/api/settings', { method: 'POST', body: patch }),
};

/** WebSocket URL carrying the auth token. */
export function wsUrl() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${location.host}/ws?token=${encodeURIComponent(TOKEN)}`;
}

/**
 * Ask the user for a directory.
 * Electron gets the real OS dialog; the browser falls back to the in-app
 * directory browser, which the caller supplies as `inAppFallback`.
 */
export async function pickDirectory(inAppFallback) {
  if (isElectron && window.codeviz.pickDirectory) {
    const chosen = await window.codeviz.pickDirectory();
    return chosen || null;
  }
  return inAppFallback ? inAppFallback() : null;
}

/** Electron menu → renderer notifications (no-ops in the browser). */
export function onShellCommand(handler) {
  if (isElectron && window.codeviz.onCommand) {
    window.codeviz.onCommand(handler);
  }
}
