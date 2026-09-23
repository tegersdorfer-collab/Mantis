# Jev vs. lokal — Mantis-Entscheidungen

92 handgelabelte Fälle (bench/jev/cases.py). Lokal = Mantis' heutige Prompts + Parser.

## Trefferquote je Entscheidungstyp

| Typ | n | qwen3.5:9b |
|---|---|---|
| intent | 26 | 18/26 |
| address | 14 | 12/14 |
| gate | 10 | 9/10 |
| proactive | 10 | 7/10 |
| inbox | 8 | 8/8 |
| task | 8 | 5/8 |
| conflict | 8 | 8/8 |
| verify | 8 | 6/8 |
| **gesamt** | | **73/92 (79%)** |

## Latenz (Sekunden, warm)

| Backend | Median | p90 | max |
|---|---|---|---|
| qwen3.5:9b | 1.34 | 2.53 | 3.02 |

## Fehler im Detail

### qwen3.5:9b — 19 falsch
- `i03` [intent] erwartet **productivity**, bekam **habits** — "Erinnere mich morgen um 9 an den Zahnarzt"
- `i09` [intent] erwartet **flipper**, bekam **robot** — "Mach die Schreibtischlampe an"
- `i11` [intent] erwartet **flipper**, bekam **chat** — "Licht aus"
- `i14` [intent] erwartet **chat**, bekam **nutrition** — "Was soll ich heute essen?"
- `i15` [intent] erwartet **habits**, bekam **fitness** — "Hab ich diese Woche schon dreimal Sport gemacht?"
- `i16` [intent] erwartet **knowledge**, bekam **nutrition** — "Wie viel Kalorien hat eine Banane?"
- `i18` [intent] erwartet **robot**, bekam **chat** — "Stopp!"
- `i20` [intent] erwartet **flipper**, bekam **robot** — "Ventilator eine Stufe höher"
- `a08` [address] erwartet **False**, bekam **True** — "hey hast du das gesehen gestern das Spiel"
- `a10` [address] erwartet **False**, bekam **True** — "wo hab ich jetzt schon wieder mein Handy hingelegt"
- `g03` [gate] erwartet **True**, bekam **False** — "Wie war mein Schlaf?"
- `p04` [proactive] erwartet **True**, bekam **False** — "Du hast gestern gesagt du willst Jonas wegen dem Fahrrad anrufen — das steht noch offen." _Offene Aktion, konkret_
- `p07` [proactive] erwartet **True**, bekam **False** — "Morgen 9:00 Zahnarzt — Praxis hat per Mail nach Versichertenkarte gefragt, liegt die noch _Neu aus Mail, konkret_
- `p10` [proactive] erwartet **True**, bekam **False** — "Der Flipper meldet seit 20 Minuten keine Verbindung mehr — die Lampe reagiert gerade nich _Systemzustand, neu, relevant_
- `t02` [task] erwartet **user**, bekam **mantis** — {"title": "Paket bei der Post abholen", "notes": "Benachrichtigungskarte liegt im Flur"}
- `t04` [task] erwartet **user**, bekam **mantis** — {"title": "Oma anrufen", "notes": "Geburtstag am Sonntag"}
- `t08` [task] erwartet **user**, bekam **mantis** — {"title": "Fahrrad zur Werkstatt bringen", "notes": null}
- `v01` [verify] erwartet **True**, bekam **False** — {"text": "Ich war heute beim Zahnarzt, war halb so wild. Danach noch kurz einkaufen.", "cl
- `v07` [verify] erwartet **True**, bekam **False** — {"text": "Kaffee brauch ich morgens nicht mehr, seit ich Tee trinke.", "claim": "Timo trin
