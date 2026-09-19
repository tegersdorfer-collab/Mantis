# Jev vs. lokal — Mantis-Entscheidungen

92 handgelabelte Fälle (bench/jev/cases.py). Lokal = Mantis' heutige Prompts + Parser.

## Trefferquote je Entscheidungstyp

| Typ | n | jev |
|---|---|---|
| intent | 26 | 23/26 |
| address | 14 | 14/14 |
| gate | 10 | 9/10 |
| proactive | 10 | 8/10 |
| inbox | 8 | 8/8 |
| task | 8 | 8/8 |
| conflict | 8 | 8/8 |
| verify | 8 | 8/8 |
| **gesamt** | | **86/92 (93%)** |

## Latenz (Sekunden, warm)

| Backend | Median | p90 | max |
|---|---|---|---|
| jev | 0.37 | 0.43 | 0.65 |

Jev-Kosten für alle 92 Calls: $0.00174 (41404 Input-Tokens).

## Kalibrierung (Jev)

Confidence = Choice-Confidence bzw. |noul−0.5|·2 bei Ja/Nein. Gut kalibriert heißt: niedrige Buckets sind auch öfter falsch.

| Confidence | n | richtig | Quote |
|---|---|---|---|
| 0.00–0.50 | 8 | 5 | 62% |
| 0.50–0.80 | 19 | 19 | 100% |
| 0.80–0.95 | 28 | 25 | 89% |
| 0.95–1.00 | 37 | 37 | 100% |

Brier-Score der 50 Ja/Nein-Fragen: 0.046 (0 = perfekt, 0.25 = Münzwurf).

## Fehler im Detail

### jev — 6 falsch
- `i14` [intent] erwartet **chat**, bekam **nutrition** conf=0.85 — "Was soll ich heute essen?"
- `i15` [intent] erwartet **habits**, bekam **fitness** conf=0.87 — "Hab ich diese Woche schon dreimal Sport gemacht?"
- `i16` [intent] erwartet **knowledge**, bekam **nutrition** conf=0.80 — "Wie viel Kalorien hat eine Banane?"
- `g07` [gate] erwartet **True**, bekam **False** conf=0.40 — "Hak Meditation ab"
- `p01` [proactive] erwartet **False**, bekam **True** conf=0.02 — "Du hast heute noch keinen Anker gesetzt und es ist 14 Uhr — willst du kurz einen setzen?" _Klingt nach Coaching/Erinnerung ohne neue Info_
- `p07` [proactive] erwartet **True**, bekam **False** conf=0.26 — "Morgen 9:00 Zahnarzt — Praxis hat per Mail nach Versichertenkarte gefragt, liegt die noch _Neu aus Mail, konkret_
