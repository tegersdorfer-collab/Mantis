import { describe, it, expect, vi } from 'vitest';
import { startVoiceCaptureStream } from './voice-capture-stream';

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  sent: unknown[] = [];
  onmessage: ((ev: { data: string }) => void) | null = null;
  onopen: (() => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  readyState = 1; // OPEN
  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }
  send(data: unknown) {
    this.sent.push(data);
  }
  close() {
    this.readyState = 3;
    this.onclose?.({ code: 1000, reason: '' } as CloseEvent);
  }
  serverClose(code = 1006, reason = '') {
    this.readyState = 3;
    this.onclose?.({ code, reason } as CloseEvent);
  }
  fail() {
    this.onerror?.(new Event('error'));
  }
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

  it('reports an unexpected close exactly once and does not report an intentional stop', () => {
    FakeWebSocket.instances = [];
    const onClosed = vi.fn();
    const stop = startVoiceCaptureStream(
      'http://localhost:7779',
      vi.fn(),
      (url) => new FakeWebSocket(url) as unknown as WebSocket,
      onClosed,
    );
    const ws = FakeWebSocket.instances[0];

    ws.serverClose(1006, 'backend restarted');
    ws.serverClose(1006, 'duplicate close');
    stop();

    expect(onClosed).toHaveBeenCalledTimes(1);
    expect(onClosed).toHaveBeenCalledWith('backend restarted');
  });

  it('reports a connection only when the WebSocket opens and only once', () => {
    FakeWebSocket.instances = [];
    const onConnected = vi.fn();
    const stop = startVoiceCaptureStream(
      'http://localhost:7779',
      vi.fn(),
      (url) => {
        const ws = new FakeWebSocket(url);
        ws.readyState = 0;
        return ws as unknown as WebSocket;
      },
      undefined,
      onConnected,
    );
    const ws = FakeWebSocket.instances[0];

    expect(onConnected).not.toHaveBeenCalled();
    ws.readyState = 1;
    ws.onopen?.();
    ws.onopen?.();

    expect(onConnected).toHaveBeenCalledTimes(1);
    stop();
  });

  it('reports a WebSocket error once even if the socket also closes', () => {
    FakeWebSocket.instances = [];
    const onClosed = vi.fn();
    startVoiceCaptureStream(
      'http://localhost:7779',
      vi.fn(),
      (url) => new FakeWebSocket(url) as unknown as WebSocket,
      onClosed,
    );
    const ws = FakeWebSocket.instances[0];

    ws.fail();
    ws.serverClose();

    expect(onClosed).toHaveBeenCalledTimes(1);
    expect(onClosed).toHaveBeenCalledWith('WebSocket error');
  });

  it('does not report a close caused by stop()', () => {
    FakeWebSocket.instances = [];
    const onClosed = vi.fn();
    const stop = startVoiceCaptureStream(
      'http://localhost:7779',
      vi.fn(),
      (url) => new FakeWebSocket(url) as unknown as WebSocket,
      onClosed,
    );

    stop();

    expect(onClosed).not.toHaveBeenCalled();
  });

  it('releases the microphone, audio contexts and streaming player after an unexpected close', async () => {
    FakeWebSocket.instances = [];
    const trackStop = vi.fn();
    const originalMediaDevices = navigator.mediaDevices;
    Object.defineProperty(navigator, 'mediaDevices', {
      value: {
        getUserMedia: async () => ({
          getTracks: () => [{ stop: trackStop }],
        } as unknown as MediaStream),
      }, configurable: true,
    });

    class FakeSource {
      buffer: AudioBuffer | null = null;
      onended: (() => void) | null = null;
      stop = vi.fn();
      connect() {}
      start() {}
    }
    class FakeAudioContext {
      static instances: FakeAudioContext[] = [];
      state = 'running';
      currentTime = 0;
      destination = {};
      source = new FakeSource();
      gain = { gain: { value: 1 }, connect() {} };
      audioWorklet = { addModule: async () => {} };
      close = vi.fn(async () => {});
      constructor() { FakeAudioContext.instances.push(this); }
      createMediaStreamSource() { return { connect() {} }; }
      createGain() { return this.gain as unknown as GainNode; }
      createBufferSource() { return this.source as unknown as AudioBufferSourceNode; }
      decodeAudioData() { return Promise.resolve({ duration: 1 } as AudioBuffer); }
      resume() { return Promise.resolve(); }
    }
    const workletDisconnect = vi.fn();
    class FakeWorkletNode {
      port = { onmessage: null as ((event: MessageEvent<ArrayBuffer>) => void) | null };
      connect() {}
      disconnect = workletDisconnect;
    }
    vi.stubGlobal('AudioContext', FakeAudioContext);
    vi.stubGlobal('AudioWorkletNode', FakeWorkletNode);
    try {
      const onClosed = vi.fn();
      startVoiceCaptureStream(
        'http://localhost:7779', vi.fn(),
        (url) => new FakeWebSocket(url) as unknown as WebSocket,
        onClosed,
      );
      await new Promise((resolve) => setTimeout(resolve, 0));
      const ws = FakeWebSocket.instances[0];
      ws.onmessage?.({ data: JSON.stringify({
        type: 'audio_chunk', seq: 0, block_id: 'turn:0', b64: btoa('audio'),
      }) });
      await new Promise((resolve) => setTimeout(resolve, 0));

      ws.serverClose(1006, 'backend restarted');

      expect(trackStop).toHaveBeenCalledTimes(1);
      expect(workletDisconnect).toHaveBeenCalledTimes(1);
      expect(FakeAudioContext.instances).toHaveLength(2);
      expect(FakeAudioContext.instances.every((context) => context.close.mock.calls.length === 1))
        .toBe(true);
      expect(FakeAudioContext.instances[1].source.stop).toHaveBeenCalledTimes(1);
      expect(onClosed).toHaveBeenCalledTimes(1);
    } finally {
      vi.unstubAllGlobals();
      Object.defineProperty(navigator, 'mediaDevices', {
        value: originalMediaDevices, configurable: true,
      });
    }
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

  it('keeps the server VAD active while a reply plays', () => {
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
      expect(ws.sent).toEqual([
        JSON.stringify({ type: 'hello', audio: 'stream', playback_progress: true }),
      ]);
    } finally {
      globalThis.Audio = originalAudio;
    }
  });

  it('uses browser echo cancellation and keeps sending microphone frames during playback', async () => {
    FakeWebSocket.instances = [];
    const workletNode: {
      current: { port: { onmessage: ((event: MessageEvent<ArrayBuffer>) => void) | null } } | null;
    } = { current: null };
    const getUserMedia = vi.fn(async (_constraints: MediaStreamConstraints) => ({
      getTracks: () => [{ stop() {} }],
    } as unknown as MediaStream));
    const originalMediaDevices = navigator.mediaDevices;
    Object.defineProperty(navigator, 'mediaDevices', {
      value: { getUserMedia }, configurable: true,
    });
    class CaptureContext {
      audioWorklet = { addModule: async () => {} };
      createMediaStreamSource() { return { connect() {} }; }
      close() { return Promise.resolve(); }
    }
    class FakeWorkletNode {
      port = { onmessage: null as ((event: MessageEvent<ArrayBuffer>) => void) | null };
      constructor() { workletNode.current = this; }
      connect() {}
    }
    const originalAudio = globalThis.Audio;
    vi.stubGlobal('AudioContext', CaptureContext);
    vi.stubGlobal('AudioWorkletNode', FakeWorkletNode);
    // @ts-expect-error - schmaler Audio-Testdouble
    globalThis.Audio = class {
      onended: (() => void) | null = null;
      onerror: (() => void) | null = null;
      play() { return new Promise<void>(() => {}); }
      pause() {}
    };
    try {
      const stop = startVoiceCaptureStream(
        'http://localhost:7779', vi.fn(),
        (url) => new FakeWebSocket(url) as unknown as WebSocket,
      );
      await new Promise((resolve) => setTimeout(resolve, 0));
      const ws = FakeWebSocket.instances[0];
      expect(getUserMedia).toHaveBeenCalledWith({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });

      ws.onmessage?.({ data: JSON.stringify({
        text: 'Antwort', addressed: true, reply: 'Hallo.', audio_b64: 'BASE64',
      }) });
      workletNode.current?.port.onmessage?.({
        data: new ArrayBuffer(8),
      } as MessageEvent<ArrayBuffer>);

      expect(ws.sent.some((item) => item instanceof ArrayBuffer)).toBe(true);
      expect(ws.sent.map(String)).not.toContain(JSON.stringify({ type: 'mute', value: true }));
      stop();
    } finally {
      globalThis.Audio = originalAudio;
      vi.unstubAllGlobals();
      Object.defineProperty(navigator, 'mediaDevices', {
        value: originalMediaDevices, configurable: true,
      });
    }
  });

  it('stops all streamed sources and reports progress before acknowledging stop_playback', async () => {
    FakeWebSocket.instances = [];
    const originalMediaDevices = navigator.mediaDevices;
    Object.defineProperty(navigator, 'mediaDevices', {
      value: {
        getUserMedia: async () => ({
          getTracks: () => [{ stop() {} }],
        } as unknown as MediaStream),
      }, configurable: true,
    });
    class FakeSource {
      buffer: AudioBuffer | null = null;
      onended: (() => void) | null = null;
      startedAt = 0;
      stopped = false;
      connect() {}
      start(at: number) { this.startedAt = at; }
      stop() { this.stopped = true; }
    }
    class PlaybackContext {
      static instances: PlaybackContext[] = [];
      state = 'running';
      currentTime = 0;
      destination = {};
      output = { gain: { value: 1 }, connect() {} };
      sources: FakeSource[] = [];
      audioWorklet = { addModule: async () => {} };
      constructor() { PlaybackContext.instances.push(this); }
      createMediaStreamSource() { return { connect() {} }; }
      createBufferSource() {
        const source = new FakeSource();
        this.sources.push(source);
        return source as unknown as AudioBufferSourceNode;
      }
      createGain() { return this.output as unknown as GainNode; }
      decodeAudioData(_data: ArrayBuffer) {
        return Promise.resolve({ duration: 1 } as AudioBuffer);
      }
      resume() { return Promise.resolve(); }
      close() { return Promise.resolve(); }
    }
    class FakeWorkletNode {
      port = { onmessage: null as ((event: MessageEvent<ArrayBuffer>) => void) | null };
      constructor() {}
      connect() {}
    }
    vi.stubGlobal('AudioContext', PlaybackContext);
    vi.stubGlobal('AudioWorkletNode', FakeWorkletNode);
    try {
      const stop = startVoiceCaptureStream(
        'http://localhost:7779', vi.fn(),
        (url) => new FakeWebSocket(url) as unknown as WebSocket,
      );
      await new Promise((resolve) => setTimeout(resolve, 0));
      const ws = FakeWebSocket.instances[0];
      ws.onmessage?.({ data: JSON.stringify({
        type: 'audio_chunk', seq: 0, block_id: 'turn:0', b64: btoa('first'),
      }) });
      ws.onmessage?.({ data: JSON.stringify({
        type: 'audio_chunk', seq: 1, block_id: 'turn:1', b64: btoa('second'),
      }) });
      await new Promise((resolve) => setTimeout(resolve, 0));
      PlaybackContext.instances[1].currentTime = 0.4;

      ws.onmessage?.({ data: JSON.stringify({ type: 'duck_playback' }) });
      expect(PlaybackContext.instances[1].output.gain.value).toBe(0.25);
      ws.onmessage?.({ data: JSON.stringify({ type: 'unduck_playback' }) });
      expect(PlaybackContext.instances[1].output.gain.value).toBe(1);

      ws.onmessage?.({ data: JSON.stringify({ type: 'stop_playback', turn_id: 'turn' }) });

      const progress = ws.sent
        .filter((item): item is string => typeof item === 'string')
        .map((item) => JSON.parse(item) as Record<string, unknown>)
        .filter((item) => item.type === 'playback_progress');
      expect(PlaybackContext.instances[1].sources.map((source) => source.stopped))
        .toEqual([true, true]);
      expect(progress).toEqual([
        { type: 'playback_progress', block_id: 'turn:0', played_ms: 400, finished: false },
        { type: 'playback_progress', block_id: 'turn:1', played_ms: 0, finished: false },
      ]);
      expect(JSON.parse(ws.sent[ws.sent.length - 1] as string)).toEqual({
        type: 'playback_stopped', turn_id: 'turn',
      });
      stop();
    } finally {
      vi.unstubAllGlobals();
      Object.defineProperty(navigator, 'mediaDevices', {
        value: originalMediaDevices, configurable: true,
      });
    }
  });

  it('starts a new streaming player at full gain after a ducked player ends', async () => {
    FakeWebSocket.instances = [];
    const originalMediaDevices = navigator.mediaDevices;
    Object.defineProperty(navigator, 'mediaDevices', {
      value: {
        getUserMedia: async () => ({
          getTracks: () => [{ stop() {} }],
        } as unknown as MediaStream),
      }, configurable: true,
    });
    class FakeSource {
      buffer: AudioBuffer | null = null;
      onended: (() => void) | null = null;
      connect() {}
      start() {}
      stop() {}
    }
    class FakeAudioContext {
      static instances: FakeAudioContext[] = [];
      state = 'running';
      currentTime = 0;
      destination = {};
      output = { gain: { value: 1 }, connect() {} };
      sources: FakeSource[] = [];
      audioWorklet = { addModule: async () => {} };
      constructor() { FakeAudioContext.instances.push(this); }
      createMediaStreamSource() { return { connect() {} }; }
      createGain() { return this.output as unknown as GainNode; }
      createBufferSource() {
        const source = new FakeSource();
        this.sources.push(source);
        return source as unknown as AudioBufferSourceNode;
      }
      decodeAudioData(_data: ArrayBuffer) {
        return Promise.resolve({ duration: 1 } as AudioBuffer);
      }
      resume() { return Promise.resolve(); }
      close() { return Promise.resolve(); }
    }
    class FakeWorkletNode {
      port = { onmessage: null as ((event: MessageEvent<ArrayBuffer>) => void) | null };
      constructor() {}
      connect() {}
    }
    vi.stubGlobal('AudioContext', FakeAudioContext);
    vi.stubGlobal('AudioWorkletNode', FakeWorkletNode);
    try {
      const stop = startVoiceCaptureStream(
        'http://localhost:7779', vi.fn(),
        (url) => new FakeWebSocket(url) as unknown as WebSocket,
      );
      await new Promise((resolve) => setTimeout(resolve, 0));
      const ws = FakeWebSocket.instances[0];
      ws.onmessage?.({ data: JSON.stringify({
        type: 'audio_chunk', seq: 0, block_id: 'old:0', b64: btoa('old'),
      }) });
      await new Promise((resolve) => setTimeout(resolve, 0));
      const oldPlayerContext = FakeAudioContext.instances?.[1];
      expect(oldPlayerContext?.output.gain.value).toBe(1);

      ws.onmessage?.({ data: JSON.stringify({ type: 'duck_playback' }) });
      expect(oldPlayerContext?.output.gain.value).toBe(0.25);
      ws.onmessage?.({ data: JSON.stringify({ type: 'audio_end', count: 1 }) });
      oldPlayerContext?.sources[0].onended?.();

      ws.onmessage?.({ data: JSON.stringify({
        type: 'audio_chunk', seq: 0, block_id: 'new:0', b64: btoa('new'),
      }) });
      await new Promise((resolve) => setTimeout(resolve, 0));

      const contexts = FakeAudioContext.instances;
      expect(contexts).toHaveLength(3);
      expect(contexts[2].output.gain.value).toBe(1);
      stop();
    } finally {
      vi.unstubAllGlobals();
      Object.defineProperty(navigator, 'mediaDevices', {
        value: originalMediaDevices, configurable: true,
      });
    }
  });
});
