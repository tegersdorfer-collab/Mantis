import { authenticatedEventSource } from './api-auth';
export type StatusEvent = {
  type: string;
  text: string;
  detail?: string | null;
  ts?: number;
};

export type EventSourceLike = {
  onmessage: ((ev: { data: string }) => void) | null;
  close(): void;
};

function defaultEsFactory(url: string): EventSourceLike {
  return authenticatedEventSource(url);
}

export function subscribeStatus(
  baseUrl: string,
  onEvent: (evt: StatusEvent) => void,
  esFactory: (url: string) => EventSourceLike = defaultEsFactory,
): () => void {
  const source = esFactory(`${baseUrl}/api/status/stream`);

  source.onmessage = (ev) => {
    try {
      const parsed = JSON.parse(ev.data);
      if (parsed && typeof parsed.type === 'string' && typeof parsed.text === 'string') {
        onEvent(parsed as StatusEvent);
      }
    } catch {
      // Kaputtes/Keepalive-Event ignorieren, Verbindung bleibt bestehen
    }
  };

  return () => source.close();
}
