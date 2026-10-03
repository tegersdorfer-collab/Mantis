# Jev vs. lokal — Mantis-Entscheidungen

92 handgelabelte Fälle (bench/jev/cases.py). Lokal = Mantis' heutige Prompts + Parser.

## Trefferquote je Entscheidungstyp

| Typ | n | clef (Cloudflare) |
|---|---|---|
| intent | 26 | 24/26 |
| address | 14 | 14/14 |
| gate | 10 | 9/10 |
| proactive | 10 | 8/10 |
| inbox | 8 | 8/8 |
| task | 8 | 8/8 |
| conflict | 8 | 7/8 |
| verify | 8 | 8/8 |
| **gesamt** | | **86/92 (93%)** |

## Latenz (Sekunden, warm)

| Backend | Median | p90 | max |
|---|---|---|---|
| clef (Cloudflare) | 0.54 | 0.85 | 1.49 |

## Fehler im Detail

### clef (Cloudflare) — 6 falsch
- `i15` [intent] erwartet **habits**, bekam **fitness** conf=0.55 — "Hab ich diese Woche schon dreimal Sport gemacht?"
- `i16` [intent] erwartet **knowledge**, bekam **nutrition** conf=0.49 — "Wie viel Kalorien hat eine Banane?"
- `g07` [gate] erwartet **True**, bekam **False** conf=0.35 — "Hak Meditation ab"
- `p01` [proactive] erwartet **False**, bekam **True** conf=0.11 — "Du hast heute noch keinen Anker gesetzt und es ist 14 Uhr — willst du kurz einen setzen?" _Klingt nach Coaching/Erinnerung ohne neue Info_
- `p10` [proactive] erwartet **True**, bekam **False** conf=0.03 — "Der Flipper meldet seit 20 Minuten keine Verbindung mehr — die Lampe reagiert gerade nich _Systemzustand, neu, relevant_
- `c02` [conflict] erwartet **False**, bekam **True** conf=0.44 — {"old": "Timo trinkt gern Kaffee", "new": "Timo trinkt gern Tee"}
