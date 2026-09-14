import { authenticatedEventSource } from './api-auth';
export type SleepNight = { date: string; hours: number | null; deep_hours: number | null };
export type WidgetSlot = { widget: string; payload: { nights: SleepNight[] } };
export type UiEvent = { layout: string | null; slots: Record<string, WidgetSlot>; ts?: number };

export type EventSourceLike = {
  onmessage: ((ev: { data: string }) => void) | null;
  close(): void;
};

function defaultEsFactory(url: string): EventSourceLike {
  return authenticatedEventSource(url);
}

export function subscribeUiState(
  baseUrl: string,
  onEvent: (evt: UiEvent) => void,
  esFactory: (url: string) => EventSourceLike = defaultEsFactory,
): () => void {
  const source = esFactory(`${baseUrl}/api/ui/stream`);

  source.onmessage = (ev) => {
    try {
      const parsed = JSON.parse(ev.data);
      onEvent(parsed as UiEvent);
    } catch {
      // Kaputtes/Keepalive-Event ignorieren, Verbindung bleibt bestehen
    }
  };

  return () => source.close();
}
