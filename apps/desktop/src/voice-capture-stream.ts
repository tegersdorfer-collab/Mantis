import { authenticatedWebSocket } from './api-auth';
import type { VoiceSegmentResult } from './voice-capture';

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

  function sendMute(value: boolean): void {
    if (ws.readyState === 1) {
      ws.send(JSON.stringify({ type: 'mute', value }));
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
      currentReplyAudio = audio;
      sendMute(true);
      // Solange Mantis spricht: Server-seitige VAD/Wake-Word-Auswertung pausieren
      // (per Mute-Flag), sonst hört das Mikrofon Mantis' eigene Stimme und löst
      // ein neues Segment aus (Echo-Vermeidung, siehe alte voice-capture.ts).
      const stopPlayingFlag = () => sendMute(false);
      audio.onended = stopPlayingFlag;
      audio.onerror = stopPlayingFlag;
      audio.play().catch(stopPlayingFlag);
    } catch {
      sendMute(false);
    }
  }

  ws.onmessage = (ev: { data: string }) => {
    try {
      const result = JSON.parse(ev.data) as VoiceSegmentResult;
      if (result.audio_b64) playReplyAudio(result.audio_b64);
      onSegment(result);
    } catch {
      // ungültige Nachricht ignorieren, kein Absturz
    }
  };

  navigator.mediaDevices
    .getUserMedia({ audio: true })
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
    if (workletNode) workletNode.port.onmessage = null;
    if (stream) stream.getTracks().forEach((t) => t.stop());
    if (audioCtx) audioCtx.close();
    ws.close();
  };
}
