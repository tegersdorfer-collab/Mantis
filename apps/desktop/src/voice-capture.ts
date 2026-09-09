import { authenticatedFetch } from './api-auth';
export type VoiceSegmentResult = {
  text: string;
  addressed: boolean;
  reply?: string | null;
  audio_b64?: string | null;
};

const SILENCE_MS_TO_STOP = 800;   // so lange Stille beendet ein Sprachsegment
const MIN_SEGMENT_MS = 300;       // kürzere "Segmente" werden verworfen (Rauschen)
const PREROLL_MS = 400;           // Vorlauf vor Lautstärke-Trigger, damit der Wortanfang nicht abgeschnitten wird
const CHUNK_TIMESLICE_MS = 100;   // Aufnahme-Intervall des durchgehenden Recorders
const BUFFER_RETENTION_MS = 4000; // wie lange gepufferte Chunks für den Vorlauf vorgehalten werden
const CALIBRATION_MS = 600;       // Dauer der Rauschpegel-Messung beim Start
const NOISE_FLOOR_MULTIPLIER = 2.5; // Schwellwert = Rauschpegel * Multiplikator
const MIN_THRESHOLD = 0.006;      // Sicherheits-Untergrenze, falls der Raum extrem leise ist

export function computeCalibratedThreshold(samples: number[]): number {
  if (samples.length === 0) return MIN_THRESHOLD;
  const mean = samples.reduce((sum, v) => sum + v, 0) / samples.length;
  return Math.max(NOISE_FLOOR_MULTIPLIER * mean, MIN_THRESHOLD);
}

type TimedChunk = { ts: number; data: Blob };

export function startVoiceCapture(
  baseUrl: string,
  onSegment: (result: VoiceSegmentResult) => void,
): () => void {
  let stopped = false;
  let stream: MediaStream | null = null;
  let audioCtx: AudioContext | null = null;
  let recorder: MediaRecorder | null = null;
  let initSegment: Blob | null = null;
  let buffer: TimedChunk[] = [];
  let speaking = false;
  let silenceStartedAt: number | null = null;
  let segmentStartedAt = 0;
  let segmentStartTs = 0;
  let rafId: number | null = null;
  let currentReplyAudio: HTMLAudioElement | null = null;
  let isPlayingReply = false;

  function extensionFor(mimeType: string): string {
    if (mimeType.includes('mp4')) return 'm4a';
    if (mimeType.includes('ogg')) return 'ogg';
    if (mimeType.includes('wav')) return 'wav';
    return 'webm';
  }

  function playReplyAudio(audioB64: string): void {
    try {
      // Vorherige Wiedergabe hart stoppen — verhindert überlappende Antworten,
      // falls ein weiteres Segment eintrifft während Mantis noch spricht.
      if (currentReplyAudio) {
        currentReplyAudio.pause();
        currentReplyAudio.onended = null;
      }
      const audio = new Audio(`data:audio/ogg;base64,${audioB64}`);
      currentReplyAudio = audio;
      isPlayingReply = true;
      // VAD während der Wiedergabe pausieren (siehe tick()) — sonst hört das
      // Mikrofon Mantis' eigene Stimme und löst sofort ein neues Segment aus.
      const stopPlayingFlag = () => {
        isPlayingReply = false;
      };
      audio.onended = stopPlayingFlag;
      audio.onerror = stopPlayingFlag;
      audio.play().catch(stopPlayingFlag);
    } catch {
      // Wiedergabe fehlgeschlagen — Text-Antwort bleibt trotzdem sichtbar
      isPlayingReply = false;
    }
  }

  async function uploadSegment(blob: Blob, mimeType: string): Promise<void> {
    try {
      const form = new FormData();
      form.append('audio', blob, `segment.${extensionFor(mimeType)}`);
      const res = await authenticatedFetch(`${baseUrl}/api/voice/segment`, { method: 'POST', body: form });
      const data = (await res.json()) as VoiceSegmentResult;
      if (data.audio_b64) playReplyAudio(data.audio_b64);
      onSegment(data);
    } catch {
      // Netzwerkfehler beim Upload — Segment geht verloren, kein Absturz
    }
  }

  function rms(data: Uint8Array): number {
    let sum = 0;
    for (let i = 0; i < data.length; i++) {
      const v = (data[i] - 128) / 128;
      sum += v * v;
    }
    return Math.sqrt(sum / data.length);
  }

  navigator.mediaDevices
    .getUserMedia({ audio: true })
    .then((mediaStream) => {
      if (stopped) {
        mediaStream.getTracks().forEach((t) => t.stop());
        return;
      }
      stream = mediaStream;
      audioCtx = new AudioContext();
      const source = audioCtx.createMediaStreamSource(stream);
      const analyser = audioCtx.createAnalyser();
      analyser.fftSize = 2048;
      source.connect(analyser);
      const data = new Uint8Array(analyser.fftSize);

      let effectiveThreshold = MIN_THRESHOLD;
      const calibrationSamples: number[] = [];
      const calibrationStart = performance.now();

      function calibrationTick(): void {
        if (stopped || !audioCtx) return;
        analyser.getByteTimeDomainData(data);
        calibrationSamples.push(rms(data));
        if (performance.now() - calibrationStart < CALIBRATION_MS) {
          rafId = requestAnimationFrame(calibrationTick);
        } else {
          effectiveThreshold = computeCalibratedThreshold(calibrationSamples);
          startRecordingAndDetection();
        }
      }

      function startRecordingAndDetection(): void {
        // Durchgehender Recorder (nie gestoppt/neugestartet pro Segment) — vermeidet, dass der
        // Wortanfang verloren geht, während MediaRecorder erst nach dem Lautstärke-Trigger anläuft.
        recorder = new MediaRecorder(stream!);
        recorder.ondataavailable = (e) => {
          const now = performance.now();
          if (!initSegment) {
            // Erster Chunk enthält bei fragmentiertem MP4 (ftyp/moov/trex) die einmalige
            // Initialisierung, ohne die spätere Fragmente nicht decodierbar sind — nie verwerfen.
            initSegment = e.data;
            return;
          }
          buffer.push({ ts: now, data: e.data });
          buffer = buffer.filter((c) => now - c.ts <= BUFFER_RETENTION_MS);
        };
        recorder.start(CHUNK_TIMESLICE_MS);

        function tick(): void {
          if (stopped || !audioCtx) return;
          if (isPlayingReply) {
            // Mikrofon-Auswertung pausiert solange Mantis selbst spricht (Echo-Vermeidung).
            rafId = requestAnimationFrame(tick);
            return;
          }
          analyser.getByteTimeDomainData(data);
          const level = rms(data);
          const now = performance.now();

          if (level > effectiveThreshold) {
            silenceStartedAt = null;
            if (!speaking) {
              speaking = true;
              segmentStartedAt = now;
              segmentStartTs = now - PREROLL_MS;
            }
          } else if (speaking) {
            if (silenceStartedAt === null) silenceStartedAt = now;
            if (now - silenceStartedAt >= SILENCE_MS_TO_STOP) {
              speaking = false;
              const duration = now - segmentStartedAt;
              if (duration >= MIN_SEGMENT_MS && recorder && initSegment) {
                const parts = [initSegment, ...buffer.filter((c) => c.ts >= segmentStartTs).map((c) => c.data)];
                const mimeType = recorder.mimeType || 'audio/webm';
                uploadSegment(new Blob(parts, { type: mimeType }), mimeType);
              }
            }
          }
          rafId = requestAnimationFrame(tick);
        }
        rafId = requestAnimationFrame(tick);
      }
      rafId = requestAnimationFrame(calibrationTick);
    })
    .catch(() => {
      // Mikrofon-Zugriff verweigert/nicht verfügbar — Voice-Capture bleibt inaktiv, kein Absturz
    });

  return () => {
    stopped = true;
    if (rafId !== null) cancelAnimationFrame(rafId);
    if (recorder && recorder.state !== 'inactive') recorder.stop();
    if (stream) stream.getTracks().forEach((t) => t.stop());
    if (audioCtx) audioCtx.close();
  };
}
