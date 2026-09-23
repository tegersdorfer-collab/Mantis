# Jev vs. lokal — Mantis-Entscheidungen

92 handgelabelte Fälle (bench/jev/cases.py). Lokal = Mantis' heutige Prompts + Parser.

## Trefferquote je Entscheidungstyp

| Typ | n | local-logits:qwen3.5:9b |
|---|---|---|
| intent | 26 | 23/26 |
| address | 14 | 10/14 |
| gate | 10 | 9/10 |
| proactive | 10 | 8/10 |
| inbox | 8 | 8/8 |
| task | 8 | 7/8 |
| conflict | 8 | 6/8 |
| verify | 8 | 6/8 |
| **gesamt** | | **77/92 (84%)** |

## Latenz (Sekunden, warm)

| Backend | Median | p90 | max |
|---|---|---|---|
| local-logits:qwen3.5:9b | 2.60 | 3.22 | 8.35 |

## Lokales Logit-Scoring

Confidence ist Noul-Abstand zu 0,5 bzw. Peakedness für Choice und Score. Abdeckung zählt Antworten mit Confidence ≥ 0,5.

| Backend | Accuracy | Noul-Brier | Median (s) | Accuracy conf≥0,5 | Abdeckung |
|---|---:|---:|---:|---:|---:|
| local-logits:qwen3.5:9b | 77/92 (84%) | 0.191 | 2.60 | 67/78 (86%) | 78/92 (85%) |

## Fehler im Detail

### local-logits:qwen3.5:9b — 15 falsch
- `i14` [intent] erwartet **chat**, bekam **nutrition** conf=0.99 — "Was soll ich heute essen?"
- `i18` [intent] erwartet **robot**, bekam **chat** conf=0.80 — "Stopp!"
- `i21` [intent] erwartet **productivity**, bekam **knowledge** conf=0.87 — "Was steht morgen an?"
- `a04` [address] erwartet **False**, bekam **True** conf=0.76 — "ja ne das passt schon so glaub ich"
- `a08` [address] erwartet **False**, bekam **True** conf=0.95 — "hey hast du das gesehen gestern das Spiel"
- `a10` [address] erwartet **False**, bekam **True** conf=0.94 — "wo hab ich jetzt schon wieder mein Handy hingelegt"
- `a14` [address] erwartet **False**, bekam **True** conf=0.78 — "der Typ gestern war echt anstrengend ey"
- `g03` [gate] erwartet **True**, bekam **False** conf=0.81 — "Wie war mein Schlaf?"
- `p04` [proactive] erwartet **True**, bekam **False** conf=0.24 — "Du hast gestern gesagt du willst Jonas wegen dem Fahrrad anrufen — das steht noch offen." _Offene Aktion, konkret_
- `p10` [proactive] erwartet **True**, bekam **False** conf=0.08 — "Der Flipper meldet seit 20 Minuten keine Verbindung mehr — die Lampe reagiert gerade nich _Systemzustand, neu, relevant_
- `t07` [task] erwartet **mantis**, bekam **user** conf=0.15 — {"title": "Termin Zahnarzt in Kalender eintragen", "notes": "Di 9 Uhr"}
- `c02` [conflict] erwartet **False**, bekam **True** conf=0.91 — {"old": "Timo trinkt gern Kaffee", "new": "Timo trinkt gern Tee"}
- `c04` [conflict] erwartet **False**, bekam **True** conf=0.84 — {"old": "Timo hat einen E-Reader", "new": "Timo hat ein Rennrad"}
- `v02` [verify] erwartet **False**, bekam **True** conf=0.36 — {"text": "Ich war heute beim Zahnarzt, war halb so wild.", "claim": "Timo hat Angst vor Za
- `v08` [verify] erwartet **False**, bekam **True** conf=0.78 — {"text": "Kaffee brauch ich morgens nicht mehr, seit ich Tee trinke.", "claim": "Timo mag 
