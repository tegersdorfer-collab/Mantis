# Jev vs. lokal — Mantis-Entscheidungen

92 handgelabelte Fälle (bench/jev/cases.py). Lokal = Mantis' heutige Prompts + Parser.

## Trefferquote je Entscheidungstyp

| Typ | n | clef-flash (Cloudflare) |
|---|---|---|
| intent | 26 | 24/26 |
| address | 14 | 14/14 |
| gate | 10 | 9/10 |
| proactive | 10 | 9/10 |
| inbox | 8 | 7/8 |
| task | 8 | 8/8 |
| conflict | 8 | 8/8 |
| verify | 8 | 7/8 |
| **gesamt** | | **86/92 (93%)** |

## Latenz (Sekunden, warm)

| Backend | Median | p90 | max |
|---|---|---|---|
| clef-flash (Cloudflare) | 0.44 | 0.75 | 1.82 |

## Fehler im Detail

### clef-flash (Cloudflare) — 6 falsch
- `i14` [intent] erwartet **chat**, bekam **nutrition** conf=0.58 — "Was soll ich heute essen?"
- `i16` [intent] erwartet **knowledge**, bekam **nutrition** conf=0.42 — "Wie viel Kalorien hat eine Banane?"
- `g07` [gate] erwartet **True**, bekam **False** conf=0.48 — "Hak Meditation ab"
- `p10` [proactive] erwartet **True**, bekam **False** conf=0.13 — "Der Flipper meldet seit 20 Minuten keine Verbindung mehr — die Lampe reagiert gerade nich _Systemzustand, neu, relevant_
- `n08` [inbox] erwartet **area**, bekam **project** conf=0.27 — {"title": "Mantis Accountability", "content": "Modul pflegen: Daily Anchor + Block laufen,
- `v01` [verify] erwartet **True**, bekam **False** conf=0.02 — {"text": "Ich war heute beim Zahnarzt, war halb so wild. Danach noch kurz einkaufen.", "cl
