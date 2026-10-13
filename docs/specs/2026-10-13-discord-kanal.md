# Spec: Discord-Kanal (Nutzungs-Challenge)

Stand 2026-10-13. Kontext: `docs/HANDOFF-nutzungs-challenge.md`. Timo hat Discord praktisch
immer offen (Handy und PC/Mac); Telegram nutzt er nur für den Bot. WhatsApp verworfen
(Sperr-Risiko, SIM mit Ausweis). Discord erlaubt Bots offiziell.

## Kleinste Version (v1)
- `communication/discord.py` nach dem Vorbild von `communication/telegram.py` und der
  Schnittstelle in `communication/base.py`. Abhängigkeit `discord.py` (2.7.x) in requirements.
- Flag `DISCORD_ENABLED` (Default `False`), Token `DISCORD_BOT_TOKEN` (nur aus `.env`, nie loggen,
  beim Laden `strip()`). Telegram bleibt unverändert an.
- **Nur Timo:** Nachrichten nur von `DISCORD_ALLOWED_USER_IDS` (kommagetrennte Snowflakes)
  verarbeiten, alles andere still ignorieren. Fail closed: leere Liste = niemandem antworten.
  Eigene Bot-Nachrichten und andere Bots immer ignorieren.
- Wo: Direktnachrichten an den Bot **und** optional ein Kanal `DISCORD_CHANNEL_ID` auf Timos
  privatem Server (dort ebenfalls nur erlaubte User). Minimal nötige Intents; DMs brauchen
  kein Message-Content-Intent, für den Server-Kanal Message Content Intent dokumentieren.
- Nur Text v1 (Anhänge/Sprachnachrichten: freundliche Ein-Satz-Antwort „kann ich noch nicht“).
- Während der Agent arbeitet: Tippanzeige (`typing()`); Antwort als fertige Nachricht.
  Discord-Limit 2000 Zeichen: sauber an Absätzen/Codeblöcken teilen. Discord-Markdown passt
  weitgehend; Tabellen in Codeblöcke oder Listen umsetzen.
- Kanal `discord` wird wie die anderen in `chat_messages` persistiert (bestehender Pfad),
  Idle-Loop-Pause/Resume über den bestehenden Turn-Zähler.
- Robustheit: discord.py-Reconnect nutzen; Login-Fehler (ungültiger Token) → `log.error`
  (redigiert), Hauptprozess läuft weiter. Läuft im selben asyncio-Loop wie der Rest.
- Benachrichtigungen (fällige Reminder): Setting `NOTIFY_CHANNEL` (Default `telegram`, erlaubt
  `discord`) → DM an den ersten erlaubten User. Keine proaktiven/autonomen Nachrichten.
- Startlog: Kanal aktiv/inaktiv, eingeloggt als <botname>.
- `docs/discord-setup.md`: Schritt-für-Schritt für Timo (Developer Portal: App + Bot anlegen,
  Token kopieren, Intents, Einladungslink mit minimalen Rechten, eigene User-ID kopieren,
  `.env`-Werte).

## Tests (TDD, discord.py gemockt)
Allowlist (fremder User, anderer Bot, leere Liste → keine Antwort), DM rein → Orchestrator →
Antwort raus, Server-Kanal nur wenn konfiguriert, Anhang → Hinweis, Teilung > 2000 Zeichen
(inkl. Codeblock über die Grenze), Persistenz mit Kanal `discord`, `NOTIFY_CHANNEL`, Flag aus →
Kanal startet nicht, ungültiger Token → kein Crash und kein Token im Log. Suite + ruff grün.

## Später (nicht v1)
Sprachnachrichten (STT vorhanden), Sprachkanal (Mantis „anrufen“), Telegram abschalten nach
einer stabilen Woche.
