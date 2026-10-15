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
): () => void {
  let stopped = false;
  let stream: MediaStream | null = null;
  let audioCtx: AudioContext | null = null;
  let workletNode: AudioWorkletNode | null = null;
  const ws = wsFactory(wsUrlFor(baseUrl));
  let currentReplyAudio: HTMLAudioElement | null = null;
  let player: StreamingAudioPlayer | null = null;
  let playbackDucked = false;

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
  if (ws.readyState === 1) sendeHello();
  else ws.onopen = sendeHello;

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

  return () => {
    stopped = true;
    playbackDucked = false;
    player?.stop();
    player = null;
    if (currentReplyAudio) {
      currentReplyAudio.pause();
      currentReplyAudio.onended = null;
      currentReplyAudio.onerror = null;
      currentReplyAudio = null;
    }
    if (workletNode) workletNode.port.onmessage = null;
    if (stream) stream.getTracks().forEach((t) => t.stop());
    if (audioCtx) audioCtx.close();
    ws.close();
  };
}
