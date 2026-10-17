import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('./voice-capture', () => ({
  startVoiceCapture: vi.fn(() => 'stop-http'),
}));
vi.mock('./voice-capture-stream', () => ({
  startVoiceCaptureStream: vi.fn(() => 'stop-ws'),
}));

// main.ts has heavy top-level side effects (subscribes to UI state, wires up
// HUD chrome, etc.) that run on import. We only need initVoiceCapture here,
// so we import it the same way main.test.ts does, relying on jsdom to
// tolerate the rest of main.ts's module-level setup.
import { initVoiceCapture } from './main';
import { startVoiceCapture } from './voice-capture';
import { startVoiceCaptureStream } from './voice-capture-stream';

describe('initVoiceCapture', () => {
  const baseUrl = 'http://localhost:9999';
  const onSegment = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal('fetch', vi.fn());
  });

  it('rejects when stream-mode lookup fails so the caller can retry and reselect the mode', async () => {
    (fetch as any).mockRejectedValue(new Error('network down'));

    await expect(initVoiceCapture(baseUrl, onSegment)).rejects.toThrow('network down');

    expect(startVoiceCapture).not.toHaveBeenCalled();
    expect(startVoiceCaptureStream).not.toHaveBeenCalled();
  });

  it('rejects a failed HTTP response instead of selecting the HTTP capture fallback', async () => {
    (fetch as any).mockResolvedValue({ ok: false, status: 503, json: async () => ({}) });

    await expect(initVoiceCapture(baseUrl, onSegment)).rejects.toThrow(
      'Voice stream mode unavailable (503)',
    );

    expect(startVoiceCapture).not.toHaveBeenCalled();
    expect(startVoiceCaptureStream).not.toHaveBeenCalled();
  });

  it('uses startVoiceCapture when the mode resolves to "http"', async () => {
    (fetch as any).mockResolvedValue({ ok: true, json: async () => ({ mode: 'http' }) });

    await initVoiceCapture(baseUrl, onSegment);

    expect(startVoiceCapture).toHaveBeenCalledWith(baseUrl, onSegment);
    expect(startVoiceCaptureStream).not.toHaveBeenCalled();
  });

  it('uses startVoiceCaptureStream when the mode resolves to "websocket"', async () => {
    (fetch as any).mockResolvedValue({ ok: true, json: async () => ({ mode: 'websocket' }) });
    const onClosed = vi.fn();
    const onConnected = vi.fn();

    await initVoiceCapture(baseUrl, onSegment, onClosed, onConnected);

    expect(startVoiceCaptureStream).toHaveBeenCalledWith(
      baseUrl, onSegment, undefined, onClosed, onConnected,
    );
    expect(startVoiceCapture).not.toHaveBeenCalled();
  });

  it('treats any non-"websocket" mode value as the http fallback', async () => {
    (fetch as any).mockResolvedValue({ ok: true, json: async () => ({ mode: 'something-else' }) });

    await initVoiceCapture(baseUrl, onSegment);

    expect(startVoiceCapture).toHaveBeenCalledWith(baseUrl, onSegment);
    expect(startVoiceCaptureStream).not.toHaveBeenCalled();
  });
});
