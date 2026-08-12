/**
 * Thin fetch wrapper + token storage.
 * Single place that knows about auth headers and error shapes.
 */

const TOKEN_KEY = 'ce.token';

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(t) {
  localStorage.setItem(TOKEN_KEY, t);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

export async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  const res = await fetch(path, {
    method: opts.method || 'GET',
    headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });

  if (res.status === 401) {
    clearToken();
    if (!location.pathname.startsWith('/login')) location.href = '/login';
    throw new Error('Session expired');
  }
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (body.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch (_) { /* keep the generic message */ }
    throw new Error(detail);
  }
  return res.json();
}

/** Subscribe to server-sent sync events. Returns a close() function. */
export function onSync(handler) {
  let source;
  try {
    source = new EventSource('/api/stream');
  } catch (_) {
    return () => {};
  }
  source.onmessage = (ev) => {
    try { handler(JSON.parse(ev.data)); } catch (_) { /* ignore keepalives */ }
  };
  source.onerror = () => { /* EventSource retries on its own */ };
  return () => source.close();
}
