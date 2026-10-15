import { authenticatedFetch } from './api-auth';
import { getBaseUrl } from './config';

export const SPEAKER_ENROLLMENT_PROMPTS = [
  'Mantis, bitte starte den Kalender und prüfe meine Termine für morgen.',
  'Ich möchte heute den aktuellen Status des Backend-Servers und der API sehen.',
  'Erstelle eine kurze Zusammenfassung vom letzten Meeting und speichere sie im Dashboard.',
  'Vergleiche die Ergebnisse aus dem letzten Deployment mit dem aktuellen Build.',
  'Suche im Knowledge Graph nach meinen Notizen zu Voice Barge-in und Speaker Verification.',
  'Wenn der Streaming-Client die Verbindung verliert, prüfe zuerst das Log und starte den Service danach erneut.',
];

const TARGET_SAMPLE_RATE = 16_000;

export function downmixAndResample(
  channels: readonly Float32Array[],
  sourceRate: number,
  targetRate = TARGET_SAMPLE_RATE,
): Float32Array {
  if (!channels.length || sourceRate <= 0 || targetRate <= 0) {
    throw new Error('Ungültige Audio-Daten');
  }
  const sourceLength = Math.min(...channels.map((channel) => channel.length));
  if (!sourceLength) throw new Error('Die Aufnahme enthält kein Audio');
  const mono = new Float32Array(sourceLength);
  for (const channel of channels) {
    for (let i = 0; i < sourceLength; i += 1) mono[i] += channel[i] / channels.length;
  }

  const targetLength = Math.max(1, Math.round(sourceLength * targetRate / sourceRate));
  const output = new Float32Array(targetLength);
  const step = sourceRate / targetRate;
  for (let i = 0; i < targetLength; i += 1) {
    const position = i * step;
    const left = Math.min(sourceLength - 1, Math.floor(position));
    const right = Math.min(sourceLength - 1, left + 1);
    const fraction = Math.min(1, position - left);
    output[i] = mono[left] * (1 - fraction) + mono[right] * fraction;
  }
  return output;
}

export function encodePcm16Wav(
  samples: Float32Array,
  sampleRate = TARGET_SAMPLE_RATE,
): Blob {
  if (sampleRate <= 0) throw new Error('Ungültige Sample-Rate');
  const dataBytes = samples.length * 2;
  const buffer = new ArrayBuffer(44 + dataBytes);
  const view = new DataView(buffer);
  const writeAscii = (offset: number, value: string): void => {
    for (let i = 0; i < value.length; i += 1) view.setUint8(offset + i, value.charCodeAt(i));
  };
  writeAscii(0, 'RIFF');
  view.setUint32(4, 36 + dataBytes, true);
  writeAscii(8, 'WAVE');
  writeAscii(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeAscii(36, 'data');
  view.setUint32(40, dataBytes, true);
  for (let i = 0; i < samples.length; i += 1) {
    const sample = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, sample < 0 ? sample * 32768 : sample * 32767, true);
  }
  return new Blob([buffer], { type: 'audio/wav' });
}

async function recorderBlobToWav(blob: Blob): Promise<Blob> {
  const context = new AudioContext({ sampleRate: TARGET_SAMPLE_RATE });
  try {
    const decoded = await context.decodeAudioData(await blob.arrayBuffer());
    const channels = Array.from({ length: decoded.numberOfChannels }, (_, index) => (
      decoded.getChannelData(index)
    ));
    const samples = downmixAndResample(channels, decoded.sampleRate);
    return encodePcm16Wav(samples, TARGET_SAMPLE_RATE);
  } finally {
    await context.close();
  }
}

type SpeakerStatus = {
  enrolled: boolean;
  embeddings: number;
  last_learning: { learned_at: string | null; score: number | null; source: string }[];
};

export function initSpeakerEnrollmentPanel(
  baseUrl = getBaseUrl(),
  fetchImpl: typeof fetch = authenticatedFetch,
): void {
  const panel = document.createElement('div');
  panel.id = 'speaker-enrollment-panel';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-modal', 'true');
  panel.setAttribute('aria-labelledby', 'speaker-enrollment-title');
  panel.innerHTML = `
    <section class="speaker-dialog">
      <header class="speaker-dialog-header">
        <div>
          <div class="speaker-eyebrow">VOICE / SECURITY</div>
          <h2 id="speaker-enrollment-title">Stimme einlernen</h2>
        </div>
        <button id="speaker-close" class="speaker-close" type="button" aria-label="Schließen">×</button>
      </header>
      <p class="speaker-intro">Lies die sechs Sätze nacheinander in normaler Lautstärke vor. Die Aufnahmen werden in 16-kHz-Mono umgewandelt und nach dem Einlernen verworfen.</p>
      <div id="speaker-profile-status" class="speaker-status" role="status">Profilstatus wird geladen …</div>
      <div id="speaker-progress" class="speaker-progress"></div>
      <blockquote id="speaker-prompt" class="speaker-prompt"></blockquote>
      <div id="speaker-action-status" class="speaker-action-status" role="status"></div>
      <div class="speaker-actions">
        <button id="speaker-record" type="button" aria-describedby="speaker-progress speaker-prompt speaker-action-status">Aufnahme starten</button>
        <button id="speaker-upload" type="button" disabled>Profil einlernen</button>
        <button id="speaker-reset" class="speaker-reset" type="button">Stimmprofil zurücksetzen</button>
      </div>
    </section>
  `;
  document.body.appendChild(panel);

  const profileStatus = panel.querySelector<HTMLElement>('#speaker-profile-status')!;
  const progress = panel.querySelector<HTMLElement>('#speaker-progress')!;
  const prompt = panel.querySelector<HTMLElement>('#speaker-prompt')!;
  const actionStatus = panel.querySelector<HTMLElement>('#speaker-action-status')!;
  const recordButton = panel.querySelector<HTMLButtonElement>('#speaker-record')!;
  const uploadButton = panel.querySelector<HTMLButtonElement>('#speaker-upload')!;
  const resetButton = panel.querySelector<HTMLButtonElement>('#speaker-reset')!;
  const clips: (Blob | null)[] = Array(SPEAKER_ENROLLMENT_PROMPTS.length).fill(null);
  let nextPrompt = 0;
  let mediaStream: MediaStream | null = null;
  let recorder: MediaRecorder | null = null;
  let recorderChunks: Blob[] = [];
  let recordingPrompt = -1;
  let discardOnStop = false;
  let recordingStarting = false;
  let enrollmentGeneration = 0;
  let returnFocus: HTMLElement | null = null;

  function updatePrompts(): void {
    const complete = clips.every(Boolean);
    progress.textContent = complete
      ? `${clips.length} von ${clips.length} Aufnahmen bereit`
      : `Satz ${nextPrompt + 1} von ${SPEAKER_ENROLLMENT_PROMPTS.length}`;
    prompt.textContent = complete
      ? 'Alle Sätze sind aufgenommen. Lerne jetzt dein Stimmprofil ein.'
      : `„${SPEAKER_ENROLLMENT_PROMPTS[nextPrompt]}“`;
    recordButton.disabled = complete;
    uploadButton.disabled = !complete;
  }

  async function refreshStatus(): Promise<void> {
    profileStatus.textContent = 'Profilstatus wird geladen …';
    try {
      const response = await fetchImpl(`${baseUrl}/api/voice/speaker`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const status = await response.json() as SpeakerStatus;
      if (status.enrolled) {
        const latest = status.last_learning?.[0];
        const learnedAt = latest?.learned_at
          ? new Date(latest.learned_at).toLocaleString()
          : null;
        const score = typeof latest?.score === 'number'
          ? ` · Score ${latest.score.toFixed(2)}` : '';
        profileStatus.textContent = [
          `Stimmprofil aktiv · ${status.embeddings} Embeddings`,
          learnedAt ? `Zuletzt gelernt: ${learnedAt}${score} (${latest?.source})` : '',
        ].filter(Boolean).join('\n');
      } else {
        profileStatus.textContent = 'Noch kein Stimmprofil eingelernt';
      }
    } catch {
      profileStatus.textContent = 'Profilstatus konnte nicht geladen werden.';
    }
  }

  function stopTracks(): void {
    mediaStream?.getTracks().forEach((track) => track.stop());
    mediaStream = null;
  }

  function close(): void {
    enrollmentGeneration += 1;
    discardOnStop = true;
    if (recorder && recorder.state !== 'inactive') recorder.stop();
    stopTracks();
    recorder = null;
    recorderChunks = [];
    clips.fill(null);
    nextPrompt = 0;
    actionStatus.textContent = '';
    panel.classList.remove('visible');
    updatePrompts();
    document.dispatchEvent(new Event('speaker-enrollment-closed'));
    returnFocus?.focus();
  }

  async function startRecording(): Promise<void> {
    if (recordingStarting) return;
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      actionStatus.textContent = 'Audioaufnahme ist in diesem Browser nicht verfügbar.';
      return;
    }
    recordingStarting = true;
    const generation = enrollmentGeneration;
    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false },
      });
      if (generation !== enrollmentGeneration) {
        stopTracks();
        recordingStarting = false;
        return;
      }
      recordingPrompt = nextPrompt;
      recorderChunks = [];
      discardOnStop = false;
      recorder = new MediaRecorder(mediaStream);
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) recorderChunks.push(event.data);
      };
      recorder.onstop = () => {
        const index = recordingPrompt;
        const raw = new Blob(recorderChunks, { type: recorder?.mimeType || 'audio/webm' });
        const discard = discardOnStop;
        stopTracks();
        recorder = null;
        recorderChunks = [];
        recordButton.textContent = 'Aufnahme starten';
        recordingStarting = false;
        if (discard || !raw.size) return;
        recordButton.disabled = true;
        void recorderBlobToWav(raw).then((wav) => {
          if (generation !== enrollmentGeneration) return;
          clips[index] = wav;
          nextPrompt = clips.findIndex((clip) => !clip);
          if (nextPrompt < 0) nextPrompt = SPEAKER_ENROLLMENT_PROMPTS.length - 1;
          actionStatus.textContent = `Satz ${index + 1} gespeichert.`;
          updatePrompts();
        }).catch(() => {
          if (generation !== enrollmentGeneration) return;
          recordButton.disabled = false;
          actionStatus.textContent = 'Die Aufnahme konnte nicht umgewandelt werden. Bitte wiederhole den Satz.';
        });
      };
      recorder.start();
      recordingStarting = false;
      recordButton.textContent = 'Aufnahme stoppen';
      actionStatus.textContent = 'Aufnahme läuft …';
    } catch {
      recordingStarting = false;
      stopTracks();
      actionStatus.textContent = 'Mikrofonzugriff fehlgeschlagen.';
    }
  }

  recordButton.addEventListener('click', () => {
    if (recorder?.state === 'recording') recorder.stop();
    else void startRecording();
  });

  uploadButton.addEventListener('click', async () => {
    const form = new FormData();
    clips.forEach((clip, index) => {
      if (clip) form.append('audio', clip, `satz-${index + 1}.wav`);
    });
    uploadButton.disabled = true;
    actionStatus.textContent = 'Stimmprofil wird eingelernt …';
    try {
      const response = await fetchImpl(`${baseUrl}/api/voice/speaker/enroll`, {
        method: 'POST', body: form,
      });
      const payload = await response.json() as SpeakerStatus & { detail?: string };
      if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
      clips.fill(null);
      nextPrompt = 0;
      updatePrompts();
      actionStatus.textContent = `Stimmprofil eingelernt · ${payload.embeddings} Embeddings.`;
      await refreshStatus();
    } catch (error) {
      actionStatus.textContent = error instanceof Error
        ? error.message : 'Einlernen fehlgeschlagen.';
      uploadButton.disabled = false;
    }
  });

  resetButton.addEventListener('click', async () => {
    if (!window.confirm('Stimmprofil wirklich zurücksetzen?')) return;
    resetButton.disabled = true;
    try {
      const response = await fetchImpl(`${baseUrl}/api/voice/speaker`, { method: 'DELETE' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      clips.fill(null);
      nextPrompt = 0;
      updatePrompts();
      actionStatus.textContent = 'Stimmprofil wurde zurückgesetzt.';
      await refreshStatus();
    } catch {
      actionStatus.textContent = 'Stimmprofil konnte nicht zurückgesetzt werden.';
    } finally {
      resetButton.disabled = false;
    }
  });

  panel.querySelector('#speaker-close')!.addEventListener('click', close);
  document.addEventListener('open-speaker-enrollment', () => {
    returnFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement : null;
    document.dispatchEvent(new Event('speaker-enrollment-opened'));
    panel.classList.add('visible');
    actionStatus.textContent = '';
    recordButton.focus();
    void refreshStatus();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && panel.classList.contains('visible')) close();
  });
  updatePrompts();
}
