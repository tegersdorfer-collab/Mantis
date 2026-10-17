export type VoiceCaptureCallbacks = {
  onClosed(reason: string): void;
  onConnected(): void;
};

type VoiceCaptureInitializer = (
  callbacks: VoiceCaptureCallbacks,
) => Promise<() => void>;

type VoiceCaptureReconnectOptions = {
  initialize: VoiceCaptureInitializer;
  onReconnecting?: (reason: string) => void;
  onConnected?: () => void;
};

const INITIAL_RETRY_MS = 1000;
const MAX_RETRY_MS = 15000;

export function createVoiceCaptureReconnect({
  initialize,
  onReconnecting,
  onConnected,
}: VoiceCaptureReconnectOptions): {
  ensure(): void;
  setEnabled(enabled: boolean): void;
} {
  let enabled = true;
  let startPending = false;
  let startRequested = false;
  let activeAttempt = 0;
  let stopVoiceCapture: (() => void) | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let nextRetryMs = INITIAL_RETRY_MS;

  function scheduleRetry(reason: string): void {
    if (!enabled || retryTimer !== null) return;
    onReconnecting?.(reason);

    const delay = nextRetryMs;
    nextRetryMs = Math.min(delay * 2, MAX_RETRY_MS);
    retryTimer = setTimeout(() => {
      retryTimer = null;
      ensure();
    }, delay);
  }

  function ensure(): void {
    if (!enabled || stopVoiceCapture || retryTimer !== null) return;
    if (startPending) {
      startRequested = true;
      return;
    }

    startPending = true;
    startRequested = false;
    const attempt = ++activeAttempt;
    let attemptClosed = false;

    let pendingStart: Promise<() => void>;
    try {
      pendingStart = initialize({
        onClosed: (reason) => {
          if (!enabled || attempt !== activeAttempt || attemptClosed) return;
          attemptClosed = true;
          stopVoiceCapture = null;
          scheduleRetry(reason);
        },
        onConnected: () => {
          if (!enabled || attempt !== activeAttempt || attemptClosed) return;
          nextRetryMs = INITIAL_RETRY_MS;
          onConnected?.();
        },
      });
    } catch (error) {
      startPending = false;
      scheduleRetry(error instanceof Error ? error.message : String(error));
      if (startRequested && enabled) {
        startRequested = false;
        ensure();
      }
      return;
    }

    void pendingStart
      .then((stop) => {
        if (!enabled || attempt !== activeAttempt || attemptClosed) {
          stop();
          return;
        }
        stopVoiceCapture = stop;
      })
      .catch((error: unknown) => {
        if (enabled && attempt === activeAttempt && !attemptClosed) {
          scheduleRetry(error instanceof Error ? error.message : String(error));
        }
      })
      .finally(() => {
        startPending = false;
        if (enabled && startRequested) {
          startRequested = false;
          ensure();
        }
      });
  }

  function setEnabled(value: boolean): void {
    if (value === enabled) {
      if (value) ensure();
      return;
    }

    enabled = value;
    if (enabled) {
      ensure();
      return;
    }

    activeAttempt += 1;
    startRequested = false;
    if (retryTimer !== null) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    const stop = stopVoiceCapture;
    stopVoiceCapture = null;
    stop?.();
  }

  return { ensure, setEnabled };
}
