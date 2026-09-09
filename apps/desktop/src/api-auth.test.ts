import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import { getApiToken, setApiToken, setBaseUrl } from './config';
import { authenticatedFetch, authenticatedEventSource, authenticatedWebSocket } from './api-auth';

beforeEach(() => { localStorage.clear(); setBaseUrl('http://localhost:7779'); });
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });
it('sends bearer only to the configured backend, retaining content headers', async () => {
  setApiToken('test-key');
  const fake = vi.fn().mockResolvedValue(new Response('{}'));
  await authenticatedFetch('http://localhost:7779/api/chat', { headers: {'Content-Type': 'application/json'} }, fake);
  const headers = new Headers(fake.mock.calls[0][1].headers);
  expect(headers.get('Authorization')).toBe('Bearer test-key');
  expect(headers.get('Content-Type')).toBe('application/json');
  await authenticatedFetch('https://example.org/api/chat', {}, fake);
  expect(new Headers(fake.mock.calls[1][1].headers).has('Authorization')).toBe(false);
});
it('keeps credentials scoped when backend address changes', () => {
  setApiToken('old-key');
  setBaseUrl('http://100.64.0.2:7779');
  expect(getApiToken()).toBe('');
});
it('streams authenticated SSE across chunks and stops on close', async () => {
  let streamController!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({start(c) { streamController = c; }});
  const fake = vi.fn().mockResolvedValue(new Response(body, { headers: {'Content-Type': 'text/event-stream'} }));
  vi.stubGlobal('fetch', fake);
  setApiToken('test-key');
  const events: string[] = [];
  const source = authenticatedEventSource('http://localhost:7779/api/status/stream');
  source.onmessage = e => events.push(e.data);
  streamController.enqueue(new TextEncoder().encode(': ping\r\ndata: {"text":'));
  streamController.enqueue(new TextEncoder().encode('"hi"}\r\n\r\n'));
  await vi.waitFor(() => expect(events).toEqual(['{"text":"hi"}']));
  expect(new Headers(fake.mock.calls[0][1].headers).get('Authorization')).toBe('Bearer test-key');
  source.close();
  expect(fake.mock.calls[0][1].signal.aborted).toBe(true);
  streamController.close();
});
it('offers websocket credentials as a subprotocol, never a URL parameter', () => {
  setApiToken('test-key');
  const sockets: unknown[][] = [];
  vi.stubGlobal('WebSocket', class { constructor(...args: unknown[]) { sockets.push(args); } });
  authenticatedWebSocket('ws://localhost:7779/ws/voice/stream');
  expect(sockets).toEqual([['ws://localhost:7779/ws/voice/stream', ['bearer.test-key']]]);
});
