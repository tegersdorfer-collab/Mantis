/**
 * Spielt die Antwort-Tonblöcke lückenlos ab, während sie noch eintreffen.
 *
 * Warum nicht `new Audio()` pro Block: zwischen zwei Audio-Elementen liegt immer
 * eine hörbare Pause (Laden, Decode, Start des nächsten Elements). Stattdessen
 * Web Audio: jeder Block wird dekodiert und per `start(zeitpunkt)` exakt an das
 * Ende des vorigen gehängt — sample-genau, ohne Lücke.
 *
 * Drei Dinge, die sonst schiefgehen:
 *
 * 1. `decodeAudioData` ist asynchron. Block 2 kann vor Block 1 fertig dekodiert
 *    sein. Deshalb landen fertige Blöcke in `wartend` und werden erst in
 *    Reihenfolge der `seq` eingeplant.
 * 2. Kommt ein Block zu spät (Netz, langsame Synthese), liegt sein geplanter
 *    Startzeitpunkt in der Vergangenheit. `Math.max(currentTime, …)` fängt das
 *    ab — dann gibt es eine kurze Lücke statt gar keinen Ton.
 * 3. Das Mikrofon bleibt während der Wiedergabe aktiv. Die Browser-
 *    Echounterdrückung schützt die Server-VAD; beim Abbruch wird der
 *    tatsächlich gespielte Fortschritt pro Block gemeldet.
 */
type WartenderBlock = { blockId: string; buffer: AudioBuffer | null };
type LaufendeQuelle = {
  quelle: AudioBufferSourceNode;
  seq: number;
  blockId: string;
  start: number;
  duration: number;
};
type PlaybackProgress = (
  blockId: string, playedMs: number, finished: boolean, skipped: boolean,
) => void;

export class StreamingAudioPlayer {
  private ctx: AudioContext | null = null;
  private naechsterStart = 0;
  private wartend = new Map<number, WartenderBlock>();
  private naechsteSeq = 0;
  private anzahlGesamt: number | null = null;
  private eingeplant = 0;
  private laufendeQuellen: LaufendeQuelle[] = [];
  private empfangeneBloecke = new Map<number, string>();
  private fortschrittGemeldet = new Set<number>();
  private fertigGemeldet = false;

  /** @param onFertig wird genau einmal gerufen, wenn der letzte Ton verklungen ist. */
  constructor(
    private onFertig: () => void,
    private onFortschritt: PlaybackProgress = () => {},
  ) {}

  private holeCtx(): AudioContext {
    if (!this.ctx) {
      // Eigener Context: der Mikrofon-Context läuft auf 16 kHz, das wäre für die
      // Wiedergabe ein hörbarer Qualitätsverlust. decodeAudioData resampelt selbst
      // auf die Rate dieses Contexts (üblicherweise 48 kHz).
      this.ctx = new AudioContext();
    }
    if (this.ctx.state === 'suspended') void this.ctx.resume();
    return this.ctx;
  }

  private meldeFortschritt(
    blockId: string, playedMs: number, finished: boolean, skipped: boolean,
  ): void {
    try {
      this.onFortschritt(blockId, playedMs, finished, skipped);
    } catch {
      // Ein geschlossener WebSocket darf das Stoppen des Audio-Graphen nicht verhindern.
    }
  }

  async push(seq: number, b64: string, blockId = String(seq)): Promise<void> {
    if (this.fertigGemeldet) return;
    this.empfangeneBloecke.set(seq, blockId);
    try {
      const ctx = this.holeCtx();
      const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
      const buffer = await ctx.decodeAudioData(bytes.buffer as ArrayBuffer);
      if (this.fertigGemeldet) return;
      this.wartend.set(seq, { blockId, buffer });
      this.planeVerfuegbare();
    } catch {
      if (this.fertigGemeldet) return;
      // Defekter Block: überspringen, sonst bliebe die Kette stehen und das
      // Mikrofon dauerhaft stumm.
      this.wartend.set(seq, { blockId, buffer: null });
      this.planeVerfuegbare();
    }
  }

  /** Meldet, wie viele Blöcke insgesamt kommen. Darf vor oder nach ihnen eintreffen. */
  markiereEnde(anzahl: number): void {
    this.anzahlGesamt = anzahl;
    if (anzahl === 0) this.meldeFertig();
    else this.pruefeFertig();
  }

  private planeVerfuegbare(): void {
    while (this.wartend.has(this.naechsteSeq)) {
      const { blockId, buffer } = this.wartend.get(this.naechsteSeq)!;
      this.wartend.delete(this.naechsteSeq);
      this.naechsteSeq += 1;
      this.eingeplant += 1;
      if (!buffer) {
        this.fortschrittGemeldet.add(this.naechsteSeq - 1);
        this.meldeFortschritt(blockId, 0, true, true);
        continue; // übersprungener Block
      }

      const ctx = this.holeCtx();
      const quelle = ctx.createBufferSource();
      quelle.buffer = buffer;
      quelle.connect(ctx.destination);
      const start = Math.max(ctx.currentTime, this.naechsterStart);
      quelle.start(start);
      this.naechsterStart = start + buffer.duration;
      const playback: LaufendeQuelle = {
        quelle, seq: this.naechsteSeq - 1, blockId, start, duration: buffer.duration,
      };
      this.laufendeQuellen.push(playback);
      quelle.onended = () => {
        this.laufendeQuellen = this.laufendeQuellen.filter((q) => q !== playback);
        this.fortschrittGemeldet.add(playback.seq);
        this.meldeFortschritt(blockId, buffer.duration * 1000, true, false);
        this.pruefeFertig();
      };
    }
    this.pruefeFertig();
  }

  private pruefeFertig(): void {
    if (this.anzahlGesamt === null) return;
    if (this.eingeplant < this.anzahlGesamt) return;
    if (this.laufendeQuellen.length > 0) return;
    this.meldeFertig();
  }

  private meldeFertig(): void {
    if (this.fertigGemeldet) return;
    this.fertigGemeldet = true;
    this.onFertig();
  }

  /** Bricht die Wiedergabe ab (z.B. weil eine neue Antwort beginnt). */
  stop(): void {
    for (const playback of this.laufendeQuellen) {
      try {
        playback.quelle.onended = null;
        playback.quelle.stop();
      } catch {
        /* bereits beendet */
      }
      const playedMs = this.ctx
        ? Math.min(
          playback.duration,
          Math.max(0, this.ctx.currentTime - playback.start),
        ) * 1000
        : 0;
      this.fortschrittGemeldet.add(playback.seq);
      this.meldeFortschritt(playback.blockId, playedMs, false, false);
    }
    for (const [seq, blockId] of this.empfangeneBloecke) {
      if (!this.fortschrittGemeldet.has(seq)) {
        this.meldeFortschritt(blockId, 0, false, false);
        this.fortschrittGemeldet.add(seq);
      }
    }
    this.laufendeQuellen = [];
    this.wartend.clear();
    this.empfangeneBloecke.clear();
    this.meldeFertig();
    if (this.ctx) {
      void this.ctx.close().catch(() => {});
      this.ctx = null;
    }
  }
}
