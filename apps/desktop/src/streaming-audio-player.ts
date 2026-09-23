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
 * 3. Das Mikrofon bleibt stummgeschaltet, bis der LETZTE Block zu Ende gespielt
 *    ist (nicht: bis er eingeplant ist). Sonst hört Mantis sich selbst und löst
 *    ein neues Segment aus.
 */
export class StreamingAudioPlayer {
  private ctx: AudioContext | null = null;
  private naechsterStart = 0;
  private wartend = new Map<number, AudioBuffer>();
  private naechsteSeq = 0;
  private anzahlGesamt: number | null = null;
  private eingeplant = 0;
  private laufendeQuellen: AudioBufferSourceNode[] = [];
  private fertigGemeldet = false;

  /** @param onFertig wird genau einmal gerufen, wenn der letzte Ton verklungen ist. */
  constructor(private onFertig: () => void) {}

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

  async push(seq: number, b64: string): Promise<void> {
    if (this.fertigGemeldet) return;
    try {
      const ctx = this.holeCtx();
      const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
      const buffer = await ctx.decodeAudioData(bytes.buffer as ArrayBuffer);
      this.wartend.set(seq, buffer);
      this.planeVerfuegbare();
    } catch {
      // Defekter Block: überspringen, sonst bliebe die Kette stehen und das
      // Mikrofon dauerhaft stumm.
      this.wartend.set(seq, null as unknown as AudioBuffer);
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
      const buffer = this.wartend.get(this.naechsteSeq)!;
      this.wartend.delete(this.naechsteSeq);
      this.naechsteSeq += 1;
      this.eingeplant += 1;
      if (!buffer) continue; // übersprungener Block

      const ctx = this.holeCtx();
      const quelle = ctx.createBufferSource();
      quelle.buffer = buffer;
      quelle.connect(ctx.destination);
      const start = Math.max(ctx.currentTime, this.naechsterStart);
      quelle.start(start);
      this.naechsterStart = start + buffer.duration;
      this.laufendeQuellen.push(quelle);
      quelle.onended = () => {
        this.laufendeQuellen = this.laufendeQuellen.filter((q) => q !== quelle);
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
    for (const q of this.laufendeQuellen) {
      try {
        q.onended = null;
        q.stop();
      } catch {
        /* bereits beendet */
      }
    }
    this.laufendeQuellen = [];
    this.wartend.clear();
    this.meldeFertig();
    if (this.ctx) {
      void this.ctx.close().catch(() => {});
      this.ctx = null;
    }
  }
}
