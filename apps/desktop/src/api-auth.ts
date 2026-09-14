import { getApiToken, getBaseUrl } from './config';

function isBackend(url: string): boolean {
  try {
    const normalized = url.replace(/^ws:/, 'http:').replace(/^wss:/, 'https:');
    return new URL(normalized).origin === new URL(getBaseUrl()).origin;
  } catch { return false; }
}

export function authenticatedFetch(
  input: RequestInfo | URL,
  init: RequestInit = {},
  fetchImpl: typeof fetch = fetch,
): Promise<Response> {
  const url = input instanceof Request ? input.url : String(input);
  const headers = new Headers(input instanceof Request ? input.headers : undefined);
  new Headers(init.headers).forEach((value, key) => headers.set(key, value));
  const token = getApiToken();
  if (token && isBackend(url)) headers.set('Authorization', `Bearer ${token}`);
  return fetchImpl(input, { ...init, headers, redirect: 'error' });
}

export function authenticatedWebSocket(url: string): WebSocket {
  const token = getApiToken();
  return new WebSocket(url, token && isBackend(url) ? [`bearer.${token}`] : []);
}

// Fetch-based SSE allows Authorization headers; credentials never enter URLs.
export function authenticatedEventSource(url: string): {
  onmessage: ((ev: { data: string }) => void) | null;
  close(): void;
} {
  let stopped = false;
  let retry: ReturnType<typeof setTimeout> | undefined;
  let controller = new AbortController();
  const source = {
    onmessage: null as ((ev: { data: string }) => void) | null,
    close() { stopped = true; clearTimeout(retry); controller.abort(); },
  };
  async function connect(): Promise<void> {
    if (stopped || !getApiToken() || !isBackend(url)) return;
    controller = new AbortController();
    try {
      const response = await authenticatedFetch(url, {
        headers: { Accept: 'text/event-stream' }, signal: controller.signal,
      });
      if (response.status === 401 || response.status === 403) return;
      if (!response.ok || !response.body) throw new Error('Stream unavailable');
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let data: string[] = [];
      try {
        while (!stopped) {
          const chunk = await reader.read();
          if (chunk.done) break;
          buffer += decoder.decode(chunk.value, { stream: true });
          let newline: number;
          while ((newline = buffer.indexOf('\n')) >= 0) {
            const line = buffer.slice(0, newline).replace(/\r$/, '');
            buffer = buffer.slice(newline + 1);
            if (!line) {
              if (data.length && !stopped) source.onmessage?.({ data: data.join('\n') });
              data = [];
            } else if (line.startsWith('data:')) {
              data.push(line.slice(5).replace(/^ /, ''));
            }
          }
        }
      } finally { reader.releaseLock(); }
    } catch { /* Reconnect after network loss; close() cancels retries. */ }
    if (!stopped) retry = setTimeout(() => void connect(), 3000);
  }
  void connect();
  return source;
}
