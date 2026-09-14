# Benchmark-Auftrag: Nanbeige4.2-3B gegen lokale Alternativen

Du sollst einen reproduzierbaren Benchmark bauen und ausführen, der Nanbeige4.2-3B
gegen die stärksten lokal lauffähigen Modelle auf dieser Maschine antreten lässt.
Zielsetzung: eine belastbare Entscheidung, ob und in welcher Rolle das Modell in
mein Projekt "Mantis" gehört (lokaler Assistent, MoE-Routing, Tool-Use).

Arbeite eigenständig. Frag nur nach, wenn eine Entscheidung nicht aus dem System
ableitbar ist. Am Ende will ich Zahlen und ein Urteil, keine Zusammenfassung
dessen, was ich schon weiß.

---

## 0. Setup und Kandidatenauswahl

1. Ermittle die Hardware: GPU + VRAM, RAM, CPU, OS. Halte das im Report fest,
   alle Ergebnisse sind ohne Hardwarekontext wertlos.
2. `ollama list` — welche Modelle sind bereits da? Nimm die als Ausgangsbasis.
3. Wähle 3–4 Gegner. Kriterien:
   - müssen bei gleicher Quantisierung (Ziel: Q4_K_M) ins VRAM/RAM passen
   - mindestens ein Modell gleicher Größenklasse (3–4B)
   - mindestens ein deutlich größeres, das gerade noch läuft (7–9B)
   - mindestens ein explizit auf Code oder Tool-Use trainiertes
   Recherchiere kurz, was aktuell in diesen Klassen als stark gilt, statt aus
   dem Gedächtnis zu raten. Begründe jede Wahl in einem Satz.
4. Zieh die offizielle Modellkarte von Nanbeige4.2-3B und übernimm die dort
   empfohlenen Inference-Parameter. Wenn ein Modell empfohlene Settings hat,
   werden die benutzt, sonst vergleichst du Äpfel mit einem falsch konfigurierten
   Modell.

**Nicht verhandelbare Fairness-Regeln:**

- Gleiche Quantisierungsstufe für alle. Wenn für ein Modell kein Q4_K_M
  existiert, dokumentiere die Abweichung explizit im Report.
- `num_ctx` wird für **jeden** Lauf explizit gesetzt. Ollama defaultet je nach
  Version auf 2048 oder 4096 und schneidet dir sonst still den halben Prompt ab.
  Das ist der häufigste Grund für kaputte Local-Benchmarks.
- Nanbeige läuft im Thinking-Mode (`preserve_thinking=true`). Die Thinking-Tokens
  zählen **voll** in die Zeitmessung, werden aber vor der Qualitätsbewertung
  entfernt. Beides ist wichtig: das Denken kostet echte Sekunden, aber bewertet
  wird die Antwort.
- Jeder Messpunkt mindestens 3 Läufe, ausgewertet wird der Median. Maschine
  ansonsten idle, keine Browser, kein Docker-Ballast.
- Modelle einzeln laden und nach jedem Block entladen. Zwei Modelle gleichzeitig
  im Speicher verfälschen jede Messung.

---

## 1. Zeit

Miss pro Modell, getrennt ausgewiesen:

- **TTFT** (Zeit bis zum ersten Token) bei kurzem Prompt
- **Prompt-Ingestion** in tok/s bei 512, 4k, 16k Tokens
- **Decode** in tok/s
- **Wall-Clock bis zur fertigen, brauchbaren Antwort** — das ist die einzige Zahl,
  die im Alltag zählt
- **Ladezeit** des Modells von kalt
- **RAM/VRAM-Peak** während des Laufs

Achte bei Nanbeige besonders auf einen Punkt: durch die Looped-Transformer-
Architektur rechnet das Modell pro Token deutlich mehr als seine Parameterzahl
vermuten lässt. Erwarte tok/s im Bereich eines 6B-Dense-Modells, nicht eines 3B.
Wenn deine Messung das nicht zeigt, prüfe ob die Loop-Konfiguration überhaupt
korrekt geladen wurde.

Rechne am Ende die theoretische Obergrenze aus Speicherbandbreite und Dateigröße
aus und setz die gemessenen tok/s ins Verhältnis. So siehst du, ob ein Modell
langsam ist oder die Maschine am Limit.

---

## 2. Kontext

1. **Needle-in-a-Haystack**: versteck eine spezifische, nicht erratbare Information
   (z.B. eine Konfigurationskonstante) bei 10%, 50% und 90% der Position, bei
   Kontextlängen von 4k, 8k, 16k, 32k. Miss die Trefferquote.
2. **Realistischer Härtetest**: gib eine echte, lange Datei aus dem Mantis-Repo
   und lass eine Änderung vornehmen, die Verständnis von zwei weit auseinander
   liegenden Stellen erfordert. Needle-Tests bestehen Modelle oft, die an echter
   Arbeit scheitern.
3. **Bruchpunkt**: erhöhe den Kontext, bis das Modell OOM geht, auf CPU
   ausgelagert wird oder die Qualität einbricht. Dokumentiere alle drei Punkte
   einzeln. Der Übergang von GPU auf CPU beim KV-Cache ist bei wenig VRAM der
   entscheidende Moment und schlägt sich brutal in der Zeit nieder.

---

## 3. Temperatur

Sweep über 0.0, 0.3, 0.7, 1.0.

- Aufgaben mit objektivem Pass/Fail (Code der kompiliert und Tests besteht,
  valides JSON) laufen zusätzlich bei Temperatur 0 als Referenz.
- Interessant ist nicht nur die Durchschnittsqualität, sondern die **Varianz**:
  drei Läufe pro Temperatur, wie stark schwanken die Ergebnisse? Ein Modell, das
  bei 0.7 mal brilliert und mal Müll produziert, ist für einen Agenten
  unbrauchbar, egal wie gut der Schnitt aussieht.
- Prüfe separat, ob die Empfehlung der Modellkarte tatsächlich das Optimum trifft.

---

## 4. Qualität

Baue pro Bereich 3–5 Aufgaben. Wo möglich automatisch verifizierbar
(Tests laufen lassen, JSON-Schema validieren), sonst bewertet.

**A. Code-Generierung**
Python und C#, jeweils eine Aufgabe mit vorgegebener Signatur und Testsuite.
Bewertung: kompiliert/läuft, besteht Tests, Lesbarkeit.

**B. Debugging**
Nimm echten Code mit eingebautem Bug (Off-by-one, Race Condition, falsche
Fehlerbehandlung). Findet das Modell den Fehler und behebt er ihn, ohne
drumherum etwas kaputtzumachen?

**C. Strukturierte Ausgabe und Tool-Calling**
Für Mantis der wichtigste Block. Gib ein JSON-Schema und mehrere Anfragen vor.
Miss die Schema-Konformität in Prozent über mindestens 20 Durchläufe. Ein Modell,
das in 5% der Fälle kaputtes JSON liefert, bricht dir jeden Agenten-Loop.
Teste zusätzlich eine Kette über 5+ aufeinanderfolgende Tool-Calls: bleibt der
Zustand konsistent oder vergisst das Modell nach dem dritten Schritt, was es tat?

**D. Instruction Following über mehrere Turns**
Gib eine Regel im ersten Turn ("antworte immer unter 3 Sätzen", "nenn niemals
Bibliothek X") und prüfe nach 8 Turns, ob sie noch gilt.

**E. Refactoring über mehrere Dateien**
Eine Änderung, die konsistent an drei Stellen greifen muss.

**F. Mantis-spezifisch**
Das ist der Block, auf den es mir ankommt. Bau die Tests aus dem echten Repo:

1. **Routing**: Der MoE-Router muss eingehende Anfragen einer Experten-Domäne
   zuordnen. Gib 30 realistische Anfragen (Fitnessdaten, Kalender, Code, Wissen,
   Small Talk) und miss die Trefferquote der Zuordnung. Vergleiche gegen das
   aktuell als Router eingesetzte Modell — wenn ein 3B-Modell hier nicht
   deutlich besser ist als ein 0.6B, lohnt der Aufpreis an Latenz nicht.
2. **Memory-Retrieval**: Gib Kontext aus dem Dual-Memory-System und eine Frage.
   Nutzt das Modell die relevanten Einträge und ignoriert es die irrelevanten?
   Erfindet es Dinge dazu?
3. **Persönlichkeits-Spec**: Das Assistenz-Profil ist "direkter Mentor, keine
   leere Bestätigung". Stell drei Fragen, die zu Schleimerei einladen, und prüfe
   ob das Modell die Spec hält oder in Standard-Assistenten-Freundlichkeit
   zurückfällt. Kleine Modelle brechen hier fast immer.
4. **Telegram-Format**: Antworten müssen kurz und ohne Markdown-Overkill sein.
   Hält das Modell das ohne ständige Erinnerung durch?
5. **COROS-Daten**: Gib einen echten Trainingsdatensatz über den MCP-Server und
   lass eine Einschätzung formulieren. Prüfe, ob die Zahlen korrekt gelesen und
   nicht halluziniert werden.

---

## 5. Bewertung

- Für alles, was nicht automatisch verifizierbar ist: **blind bewerten**. Speichere
  die Outputs anonymisiert, misch die Reihenfolge und bewerte erst danach. Wenn du
  weißt, welches Modell geantwortet hat, ist deine Bewertung wertlos.
- Definiere die Bewertungskriterien pro Bereich **vor** dem ersten Lauf und leg
  sie im Repo ab.
- Wo möglich, lass die Bewertung zusätzlich von einem stärkeren Modell gegenprüfen
  und dokumentiere Abweichungen.

---

## 6. Ergebnis

Leg im Repo ab:

- `bench/` mit allen Skripten, so dass ich den Lauf mit einem Befehl wiederholen kann
- Rohdaten als JSON, damit ich später ein neues Modell dazunehmen kann ohne alles
  neu zu messen
- `bench/RESULTS.md` mit:
  - Hardware- und Konfigurationsangaben
  - Tabelle Zeit, Tabelle Qualität pro Bereich, Kontext-Bruchpunkte,
    Temperatur-Kurven inklusive Varianz
  - eine klare Aussage, wo Nanbeige gewinnt und wo es verliert
  - **Empfehlung**: taugt es als Router, als Executor, als beides, oder gar nicht?
    Falls nicht: welches der getesteten Modelle stattdessen?
  - Was du beim Messen nicht sauber isolieren konntest. Diese Ehrlichkeit ist mir
    wichtiger als eine vollständig wirkende Tabelle.

Erwarte nicht, dass das Ergebnis die Benchmark-Zahlen der Modellkarte bestätigt.
Die sind vom Hersteller selbst gemessen, teilweise mit eigenen Scaffolds, und nicht
unabhängig reproduziert. Wenn deine Messung stark abweicht, ist das ein Ergebnis,
kein Fehler.
