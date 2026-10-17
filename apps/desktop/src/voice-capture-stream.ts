import { authenticatedWebSocket } from './api-auth';
import type { VoiceSegmentResult } from './voice-capture';
import { StreamingAudioPlayer } from './streaming-audio-player';

function wsUrlFor(baseUrl: string): string {
  return baseUrl.replace(/^http/, 'ws') + '/ws/voice/stream';
}

export function startVoiceCaptureStream(
  baseUrl: string,
  onSegment: (result: VoiceSegmentResult) => void,
  wsFactory: (url: string) => WebSocket = authenticatedWebSocket,
  onClosed?: (reason: string) => void,
  onConnected?: () => void,
): () => void {
  let stopped = false;
  let connected = false;
  let stream: MediaStream | null = null;
  let audioCtx: AudioContext | null = null;
  let workletNode: AudioWorkletNode | null = null;
  const ws = wsFactory(wsUrlFor(baseUrl));
  let currentReplyAudio: HTMLAudioElement | null = null;
  let player: StreamingAudioPlayer | null = null;
  let playbackDucked = false;

  function cleanup(closeSocket: boolean): void {
    if (stopped) return;
    stopped = true;
    playbackDucked = false;
    try { player?.stop(); } catch { /* cleanup continues for the remaining resources */ }
    player = null;
    if (currentReplyAudio) {
      try { currentReplyAudio.pause(); } catch { /* element may already be detached */ }
      currentReplyAudio.onended = null;
      currentReplyAudio.onerror = null;
      currentReplyAudio = null;
    }
    if (workletNode) {
      workletNode.port.onmessage = null;
      try { workletNode.disconnect(); } catch { /* context may already be closed */ }
      workletNode = null;
    }
    if (stream) {
      try {
        stream.getTracks().forEach((track) => {
          try { track.stop(); } catch { /* continue releasing the other tracks */ }
        });
      } catch { /* stream may already be unavailable */ }
      stream = null;
    }
    if (audioCtx) {
      const context = audioCtx;
      audioCtx = null;
      try { void context.close().catch(() => {}); } catch { /* context may already be closed */ }
    }
    ws.onmessage = null;
    ws.onopen = null;
    ws.onclose = null;
    ws.onerror = null;
    if (closeSocket && ws.readyState < 2) {
      try { ws.close(); } catch { /* socket already closed */ }
    }
  }

  function reportConnected(): void {
    if (stopped || connected) return;
    connected = true;
    onConnected?.();
  }

  function reportClosed(reason: string, closeSocket: boolean): void {
    if (stopped) return;
    cleanup(closeSocket);
    try { onClosed?.(reason); } catch { /* reconnect callback must not disrupt cleanup */ }
  }

  function duckPlayback(): void {
    playbackDucked = true;
    player?.duck();
    if (currentReplyAudio) currentReplyAudio.volume = 0.25;
  }

  function unduckPlayback(): void {
    playbackDucked = false;
    player?.unduck();
    if (currentReplyAudio) currentReplyAudio.volume = 1;
  }

  function sendProgress(
    blockId: string, playedMs: number, finished: boolean, skipped: boolean,
  ): void {
    if (ws.readyState === 1) {
      ws.send(JSON.stringify({
        type: 'playback_progress', block_id: blockId,
        played_ms: playedMs, finished, ...(skipped ? { skipped: true } : {}),
      }));
    }
  }

  function playReplyAudio(audioB64: string): void {
    try {
      if (currentReplyAudio) {
        currentReplyAudio.pause();
        currentReplyAudio.onended = null;
        currentReplyAudio.onerror = null;
      }
      const audio = new Audio(`data:audio/ogg;base64,${audioB64}`);
      audio.volume = playbackDucked ? 0.25 : 1;
      currentReplyAudio = audio;
      audio.play().catch(() => {});
    } catch { /* nicht verfügbare Audioausgabe ignorieren */ }
  }

  function startStreamingPlayback(): StreamingAudioPlayer {
    player?.stop();
    const nextPlayer = new StreamingAudioPlayer(() => {
      if (player !== nextPlayer) return;
      player = null;
      playbackDucked = false;
    }, sendProgress);
    player = nextPlayer;
    if (playbackDucked) player.duck();
    return player;
  }

  ws.onmessage = (ev: { data: string }) => {
    try {
      const msg = JSON.parse(ev.data) as Record<string, unknown>;

      if (msg.type === 'duck_playback') {
        duckPlayback();
        return;
      }
      if (msg.type === 'unduck_playback') {
        unduckPlayback();
        return;
      }
      if (msg.type === 'stop_playback') {
        playbackDucked = false;
        player?.stop();
        player = null;
        if (currentReplyAudio) {
          currentReplyAudio.volume = 1;
          currentReplyAudio.pause();
          currentReplyAudio.onended = null;
          currentReplyAudio.onerror = null;
          currentReplyAudio = null;
        }
        if (ws.readyState === 1) {
          ws.send(JSON.stringify({ type: 'playback_stopped', turn_id: msg.turn_id }));
        }
        return;
      }

      if (msg.type === 'audio_chunk') {
        (player ?? startStreamingPlayback())
          .push(msg.seq as number, msg.b64 as string, String(msg.block_id ?? msg.seq))
          .catch(() => {});
        return;
      }
      if (msg.type === 'audio_end') {
        (player ?? startStreamingPlayback()).markiereEnde(msg.count as number);
        return;
      }

      const result = msg as unknown as VoiceSegmentResult;
      if (result.audio_streaming) {
        // Tonblöcke folgen gleich. Der Worklet streamt das Mikrofon parallel weiter.
        startStreamingPlayback();
      } else if (result.audio_b64) {
        playReplyAudio(result.audio_b64);
      }
      onSegment(result);
    } catch {
      // ungültige Nachricht ignorieren, kein Absturz
    }
  };

  // Fortschritt ist opt-in; ältere Server können das zusätzliche Feld ignorieren.
  const sendeHello = () => {
    if (ws.readyState === 1) {
      ws.send(JSON.stringify({
        type: 'hello', audio: 'stream', playback_progress: true,
      }));
    }
  };
  ws.onclose = (event: CloseEvent) => {
    const reason = event.reason || `WebSocket closed (${event.code})`;
    reportClosed(reason, false);
  };
  ws.onerror = () => reportClosed('WebSocket error', true);
  if (ws.readyState === 1) {
    sendeHello();
    reportConnected();
  } else {
    ws.onopen = () => {
      sendeHello();
      reportConnected();
    };
  }

  navigator.mediaDevices
    .getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    })
    .then(async (mediaStream) => {
      if (stopped) {
        mediaStream.getTracks().forEach((t) => t.stop());
        return;
      }
      stream = mediaStream;
      audioCtx = new AudioContext({ sampleRate: 16000 });
      await audioCtx.audioWorklet.addModule('/pcm-worklet.js');
      const source = audioCtx.createMediaStreamSource(stream);
      workletNode = new AudioWorkletNode(audioCtx, 'pcm-worklet');
      workletNode.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
        if (!stopped && ws.readyState === 1) {
          ws.send(event.data);
        }
      };
      source.connect(workletNode);
    })
    .catch(() => {
      // Mikrofon-Zugriff verweigert/nicht verfügbar — Voice-Capture bleibt inaktiv
    });

  return () => cleanup(true);
}
