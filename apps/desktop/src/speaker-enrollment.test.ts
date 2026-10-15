import { describe, expect, it } from 'vitest';
import {
  SPEAKER_ENROLLMENT_PROMPTS,
  downmixAndResample,
  encodePcm16Wav,
} from './speaker-enrollment';

describe('speaker enrollment helpers', () => {
  it('liefert sechs deutsche Übungssätze mit englischen Fachwörtern', () => {
    expect(SPEAKER_ENROLLMENT_PROMPTS).toHaveLength(6);
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
});
