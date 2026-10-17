import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  createVoiceCaptureReconnect,
  type VoiceCaptureCallbacks,
} from './voice-capture-reconnect';

type CloseHandler = (reason: string) => void;
type ConnectHandler = () => void;

async function flushPromises(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

describe('createVoiceCaptureReconnect', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('retries failed initialization with exponential delays capped at fifteen seconds', async () => {
    vi.useFakeTimers();
    const initialize = vi.fn(async () => { throw new Error('backend offline'); });
    const reconnect = createVoiceCaptureReconnect({ initialize });

    reconnect.ensure();
    await flushPromises();
    expect(initialize).toHaveBeenCalledTimes(1);

    const delays = [1000, 2000, 4000, 8000, 15000, 15000];
    for (const [index, delay] of delays.entries()) {
      vi.advanceTimersByTime(delay - 1);
      expect(initialize).toHaveBeenCalledTimes(index + 1);
      vi.advanceTimersByTime(1);
      await flushPromises();
      expect(initialize).toHaveBeenCalledTimes(index + 2);
    }
  });

  it('retries a closed stream and resets the delay after the next connection opens', async () => {
    vi.useFakeTimers();
    let onClosed: CloseHandler | undefined;
    let onConnected: ConnectHandler | undefined;
    let attempts = 0;
    const initialize = vi.fn((callbacks: VoiceCaptureCallbacks) => {
      attempts += 1;
      onClosed = callbacks.onClosed;
      onConnected = callbacks.onConnected;
      if (attempts === 1) {
        return Promise.reject(new Error('backend offline'));
      }
      return Promise.resolve(() => {});
    });
    const reconnect = createVoiceCaptureReconnect({ initialize });

    reconnect.ensure();
    await flushPromises();
    vi.advanceTimersByTime(1000);
    await flushPromises();
    expect(initialize).toHaveBeenCalledTimes(2);

    onConnected?.();
    onClosed?.('backend restarted');
    vi.advanceTimersByTime(999);
    expect(initialize).toHaveBeenCalledTimes(2);
    vi.advanceTimersByTime(1);
    await flushPromises();
    expect(initialize).toHaveBeenCalledTimes(3);
  });

  it('does not start twice while initialization is pending and stops a late result when disabled', async () => {
    let resolveStart!: (stop: () => void) => void;
    const stop = vi.fn();
    const initialize = vi.fn(() => new Promise<() => void>((resolve) => {
      resolveStart = resolve;
    }));
    const reconnect = createVoiceCaptureReconnect({ initialize });

    reconnect.ensure();
    reconnect.ensure();
    expect(initialize).toHaveBeenCalledTimes(1);

    reconnect.setEnabled(false);
    resolveStart(stop);
    await flushPromises();
    expect(stop).toHaveBeenCalledTimes(1);
  });
});
