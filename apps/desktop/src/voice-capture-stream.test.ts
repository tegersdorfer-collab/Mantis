import { describe, it, expect, vi } from 'vitest';
import { startVoiceCaptureStream } from './voice-capture-stream';

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  sent: unknown[] = [];
  onmessage: ((ev: { data: string }) => void) | null = null;
  onopen: (() => void) | null = null;
  readyState = 1; // OPEN
  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }
  send(data: unknown) {
    this.sent.push(data);
  }
  close() {}
}

describe('startVoiceCaptureStream', () => {
  it('opens a WebSocket to the stream endpoint', () => {
    FakeWebSocket.instances = [];
    const onSegment = vi.fn();
    const stop = startVoiceCaptureStream(
      'http://localhost:7779',
      onSegment,
      (url) => new FakeWebSocket(url) as unknown as WebSocket,
    );
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(FakeWebSocket.instances[0].url).toBe('ws://localhost:7779/ws/voice/stream');
    stop();
  });

  it('forwards segment_result messages to onSegment', () => {
    FakeWebSocket.instances = [];
    const onSegment = vi.fn();
    startVoiceCaptureStream(
      'http://localhost:7779',
      onSegment,
      (url) => new FakeWebSocket(url) as unknown as WebSocket,
    );
    const ws = FakeWebSocket.instances[0];
    const payload = { text: 'hallo', addressed: true, reply: 'hi', audio_b64: null };
    ws.onmessage?.({ data: JSON.stringify(payload) });
    expect(onSegment).toHaveBeenCalledWith(payload);
  });

  it('sends a mute=true control message while a reply plays', () => {
    FakeWebSocket.instances = [];
    const originalAudio = globalThis.Audio;
    // @ts-expect-error - Test-Double, kein vollständiges HTMLAudioElement nötig
    globalThis.Audio = class {
      onended: (() => void) | null = null;
      onerror: (() => void) | null = null;
      play() {
        return Promise.resolve();
      }
      pause() {}
    };
    try {
      const onSegment = vi.fn();
      startVoiceCaptureStream(
        'http://localhost:7779',
        onSegment,
        (url) => new FakeWebSocket(url) as unknown as WebSocket,
      );
      const ws = FakeWebSocket.instances[0];
      const payload = { text: 'hallo', addressed: true, reply: 'hi', audio_b64: 'BASE64AUDIO' };
      ws.onmessage?.({ data: JSON.stringify(payload) });
      // Seit dem Streaming-Umbau meldet der Client zuerst an, dass er Tonblöcke
      // verarbeiten kann; danach folgt wie bisher die Stummschaltung.
      expect(ws.sent).toEqual([
        JSON.stringify({ type: 'hello', audio: 'stream' }),
        JSON.stringify({ type: 'mute', value: true }),
      ]);
    } finally {
      globalThis.Audio = originalAudio;
    }
  });
});
