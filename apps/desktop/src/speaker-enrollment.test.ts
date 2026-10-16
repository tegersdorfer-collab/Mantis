import { describe, expect, it } from 'vitest';
import {
  SPEAKER_ENROLLMENT_PROMPTS,
  downmixAndResample,
  encodePcm16Wav,
} from './speaker-enrollment';
import * as speakerEnrollment from './speaker-enrollment';

describe('speaker enrollment helpers', () => {
  it('liefert zwölf deutsche Übungssätze mit englischen Fachwörtern', () => {
    expect(SPEAKER_ENROLLMENT_PROMPTS).toHaveLength(12);
    const text = SPEAKER_ENROLLMENT_PROMPTS.join(' ').toLowerCase();
    expect(text).toContain('backend');
    expect(text).toContain('api');
    expect(text).toContain('build');
    expect(text).toContain('streaming');
  });

  it('mischt Kanäle und resampelt auf die gewünschte Rate', () => {
    const left = Float32Array.from([1, -1]);
    const right = Float32Array.from([1, -1]);
    const result = downmixAndResample([left, right], 8_000, 16_000);

    expect(result).toHaveLength(4);
    expect([...result]).toEqual([1, 0, -1, -1]);
  });

  it('schreibt mono PCM16 WAV mit 16-kHz-Header', async () => {
    const wav = encodePcm16Wav(Float32Array.from([0, 0.5, -1]), 16_000);
    const bytes = new DataView(await wav.arrayBuffer());

    expect(wav.type).toBe('audio/wav');
    expect(bytes.getUint32(24, true)).toBe(16_000);
    expect(bytes.getUint16(22, true)).toBe(1);
    expect(bytes.getUint16(34, true)).toBe(16);
    expect(bytes.byteLength).toBe(50);
  });

  it('lädt WAV-Clips einzeln hoch und schließt danach die Sitzung ab', async () => {
    type UploadClips = (
      baseUrl: string,
      clips: readonly Blob[],
      fetchImpl: typeof fetch,
      sessionId: string,
    ) => Promise<{ enrolled: boolean; embeddings: number; last_learning: unknown[] }>;
    const uploadClips = (speakerEnrollment as unknown as {
      uploadSpeakerEnrollmentClips: UploadClips;
    }).uploadSpeakerEnrollmentClips;
    expect(uploadClips).toBeTypeOf('function');

    const clips = [
      new Blob(['wav-1'], { type: 'audio/wav' }),
      new Blob(['wav-2'], { type: 'audio/wav' }),
    ];
    const calls: { url: string; init?: RequestInit }[] = [];
    const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      const result = String(input).includes('/finish')
        ? { enrolled: true, embeddings: 2, last_learning: [] }
        : { clips: calls.length };
      return new Response(JSON.stringify(result), { status: 200 });
    }) as typeof fetch;

    const result = await uploadClips(
      'http://mantis.local', clips, fetchImpl, 'session-123',
    );

    expect(result.embeddings).toBe(2);
    expect(calls.map(({ url }) => url)).toEqual([
      'http://mantis.local/api/voice/speaker/enroll/clip?session_id=session-123',
      'http://mantis.local/api/voice/speaker/enroll/clip?session_id=session-123',
      'http://mantis.local/api/voice/speaker/enroll/finish?session_id=session-123',
    ]);
    expect(calls.slice(0, 2).map(({ init }) => init?.method)).toEqual(['POST', 'POST']);
    expect(calls.slice(0, 2).map(({ init }) => init?.body)).toEqual(clips);
    expect(calls[2].init?.method).toBe('POST');
    expect(calls[2].init?.body).toBeUndefined();
  });
});
