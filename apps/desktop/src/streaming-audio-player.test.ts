import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { StreamingAudioPlayer } from './streaming-audio-player';

/** Merkt sich, wann jede Quelle gestartet wurde — damit prüfbar ist, ob lückenlos gereiht wird. */
class FakeSource {
  buffer: { duration: number } | null = null;
  onended: (() => void) | null = null;
  startedAt: number | null = null;
  stopped = false;
  connect() {}
  start(at: number) {
    this.startedAt = at;
    FakeContext.current!.gestartet.push(this);
  }
  stop() {
    this.stopped = true;
  }
}

class FakeContext {
  static current: FakeContext | null = null;
  state = 'running';
  currentTime = 0;
  destination = {};
  gestartet: FakeSource[] = [];
  closed = false;
  /** Dauer je dekodiertem Block, in Reihenfolge der decodeAudioData-Aufrufe. */
  dauern: number[] = [];
  fehlerBeiDecode = new Set<number>();
  private decodeCount = 0;

  constructor() {
    FakeContext.current = this;
  }
  createBufferSource() {
    return new FakeSource() as unknown as AudioBufferSourceNode;
  }
  decodeAudioData(_buf: ArrayBuffer): Promise<AudioBuffer> {
    const i = this.decodeCount++;
    if (this.fehlerBeiDecode.has(i)) return Promise.reject(new Error('kaputt'));
    return Promise.resolve({ duration: this.dauern[i] ?? 1 } as unknown as AudioBuffer);
  }
  resume() {
    this.state = 'running';
    return Promise.resolve();
  }
  close() {
    this.closed = true;
    return Promise.resolve();
  }
}

const b64 = (s: string) => btoa(s);  // jsdom bringt btoa/atob mit — kein @types/node nötig

beforeEach(() => {
  FakeContext.current = null;
  vi.stubGlobal('AudioContext', FakeContext);
});
afterEach(() => vi.unstubAllGlobals());

/** Wartet, bis alle anhängigen decodeAudioData-Promises abgearbeitet sind. */
const flush = () => new Promise((r) => setTimeout(r, 0));

describe('StreamingAudioPlayer', () => {
  it('reiht Blöcke lückenlos aneinander', async () => {
    const p = new StreamingAudioPlayer(() => {});
    await p.push(0, b64('a'));
    const ctx = FakeContext.current!;
    await p.push(1, b64('b'));
    await flush();

    const [erste, zweite] = ctx.gestartet;
    // Zweiter Block startet exakt am Ende des ersten — keine Lücke, keine Überlappung.
    expect(zweite.startedAt).toBeCloseTo(erste.startedAt! + erste.buffer!.duration, 5);
  });

  it('spielt in seq-Reihenfolge, auch wenn Blöcke verdreht ankommen', async () => {
    const p = new StreamingAudioPlayer(() => {});
    await p.push(0, b64('x'));
    const ctx = FakeContext.current!;
    // Block 2 trifft vor Block 1 ein (langsamerer Decode).
    await p.push(2, b64('z'));
    await flush();
    expect(ctx.gestartet.length).toBe(1); // 2 wartet auf 1

    await p.push(1, b64('y'));
    await flush();
    expect(ctx.gestartet.length).toBe(3);
  });

  it('meldet erst fertig, wenn der letzte Ton verklungen ist', async () => {
    const fertig = vi.fn();
    const p = new StreamingAudioPlayer(fertig);
    await p.push(0, b64('a'));
    await flush();
    p.markiereEnde(1);
    // Eingeplant ist nicht gespielt: das Mikrofon muss bis zum Ende stumm bleiben.
    expect(fertig).not.toHaveBeenCalled();

    FakeContext.current!.gestartet[0].onended!();
    expect(fertig).toHaveBeenCalledTimes(1);
  });

  it('meldet fertig auch ohne einen einzigen Block', () => {
    const fertig = vi.fn();
    const p = new StreamingAudioPlayer(fertig);
    p.markiereEnde(0); // TTS fehlgeschlagen — Mikrofon darf nicht stumm bleiben
    expect(fertig).toHaveBeenCalledTimes(1);
  });

  it('überspringt einen defekten Block statt hängenzubleiben', async () => {
    const fertig = vi.fn();
    const p = new StreamingAudioPlayer(fertig);
    await p.push(0, b64('a'));
    const ctx = FakeContext.current!;
    ctx.fehlerBeiDecode.add(1); // zweiter Decode schlägt fehl
    await p.push(1, b64('b'));
    await p.push(2, b64('c'));
    await flush();
    p.markiereEnde(3);

    expect(ctx.gestartet.length).toBe(2); // Block 1 fehlt, 0 und 2 laufen
    ctx.gestartet.forEach((q) => q.onended!());
    expect(fertig).toHaveBeenCalledTimes(1); // trotzdem sauber beendet
  });

  it('startet einen verspäteten Block sofort statt in der Vergangenheit', async () => {
    const p = new StreamingAudioPlayer(() => {});
    await p.push(0, b64('a'));
    const ctx = FakeContext.current!;
    ctx.dauern = [1, 1];
    await flush();
    ctx.currentTime = 10; // Block 1 kam viel zu spät
    await p.push(1, b64('b'));
    await flush();

    const zweite = ctx.gestartet[1];
    expect(zweite.startedAt).toBeGreaterThanOrEqual(10);
  });

  it('stop() bricht ab und meldet fertig, damit das Mikrofon freigegeben wird', async () => {
    const fertig = vi.fn();
    const p = new StreamingAudioPlayer(fertig);
    await p.push(0, b64('a'));
    await flush();
    const ctx = FakeContext.current!;
    p.stop();
    expect(ctx.gestartet[0].stopped).toBe(true);
    expect(fertig).toHaveBeenCalledTimes(1);
    expect(ctx.closed).toBe(true);
  });

  it('meldet fertig nur ein einziges Mal', async () => {
    const fertig = vi.fn();
    const p = new StreamingAudioPlayer(fertig);
    await p.push(0, b64('a'));
    await flush();
    p.markiereEnde(1);
    FakeContext.current!.gestartet[0].onended!();
    p.stop();
    expect(fertig).toHaveBeenCalledTimes(1);
  });
});
