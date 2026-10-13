# Discord-Kanal einrichten

Der Discord-Bot ergänzt Telegram. Telegram bleibt für den Start weiterhin erforderlich. Der
Discord-Kanal ist standardmäßig deaktiviert und verarbeitet ausschließlich Nachrichten von
IDs in `DISCORD_ALLOWED_USER_IDS`.

## 1. Bot im Developer Portal anlegen

1. Öffne das [Discord Developer Portal](https://discord.com/developers/applications) und wähle
   **New Application**.
2. Vergib einen Namen für Mantis und öffne danach den Bereich **Bot**.
3. Erstelle den Bot, falls Discord ihn noch nicht angelegt hat. Kopiere unter **Reset Token** den
   Bot-Token direkt in deine lokale `.env`. Teile ihn nicht und speichere ihn nicht im Repository.
4. Unter **Privileged Gateway Intents** brauchst du nur **Message Content Intent**, wenn du
   zusätzlich einen Serverkanal über `DISCORD_CHANNEL_ID` verwendest. Für DMs bleibt dieser Intent
   aus. Server Member Intent und Presence Intent bleiben aus.

## 2. Bot in den privaten Server einladen

Für DMs allein ist keine Server-Einladung nötig. Für einen Serverkanal:

1. Öffne im Developer Portal **OAuth2 → URL Generator**.
2. Wähle nur den Scope **bot**.
3. Wähle als Bot-Rechte **View Channels** und **Send Messages**. Gib dem Bot keine Administrator-
   Rechte. Beschränke den Kanalzugriff im Server zusätzlich auf den privaten Kanal.
4. Öffne den erzeugten Link und füge den Bot zu deinem privaten Server hinzu.
5. Aktiviere in Discord unter **User Settings → Advanced → Developer Mode** den Entwicklermodus.
   Kopiere mit Rechtsklick auf dein Profil deine User-ID. Wenn du einen Serverkanal verwendest,
   kopiere ebenso dessen Kanal-ID.

Der Bot erhält Nachrichten aus DMs ohne Message Content Intent. Ein Serverkanal braucht sowohl
den aktivierten Intent im Developer Portal als auch `DISCORD_CHANNEL_ID` in der `.env`; Mantis
verarbeitet dort weiterhin nur Nachrichten von `DISCORD_ALLOWED_USER_IDS`.

## 3. `.env` konfigurieren

Ergänze die vorhandene `.env` mit diesen Werten. Ersetze die Platzhalter lokal und lass den Token
aus Logs, Screenshots und Git heraus:

```dotenv
DISCORD_ENABLED=true
DISCORD_BOT_TOKEN=DEIN_BOT_TOKEN
DISCORD_ALLOWED_USER_IDS=DEINE_DISCORD_USER_ID
DISCORD_CHANNEL_ID=
NOTIFY_CHANNEL=telegram
```

`DISCORD_ALLOWED_USER_IDS` ist erforderlich. Mehrere IDs können kommasepariert werden; eine leere
Liste bedeutet, dass Mantis niemandem antwortet. Für DMs bleibt `DISCORD_CHANNEL_ID` leer. Wenn
Mantis auch in einem Serverkanal antworten soll, trage dort dessen ID ein und aktiviere im Portal
den Message Content Intent:

```dotenv
DISCORD_CHANNEL_ID=DEINE_SERVERKANAL_ID
```

Fällige Reminder gehen standardmäßig weiter an Telegram. Wenn sie als DM an den ersten Eintrag in
`DISCORD_ALLOWED_USER_IDS` gehen sollen, setze:

```dotenv
NOTIFY_CHANNEL=discord
```

Erstnachrichten an den Bot kannst du direkt per DM senden. Telegram-Token und übrige bestehende
Einstellungen bleiben unverändert. Nach einer `.env`-Änderung Mantis regulär neu starten.

## 4. Betrieb prüfen

Beim Start erscheint `Discord-Kanal aktiv`, wenn der Kanal aktiviert und der Token vorhanden ist.
Nach erfolgreichem Login folgt `Discord eingeloggt als <Botname>`. Bei deaktiviertem Kanal steht
`Discord-Kanal inaktiv`. Ein ungültiger Token wird redigiert als Fehler protokolliert; Mantis und
Telegram laufen weiter.

Wenn der Bot nicht reagiert, prüfe zuerst:

- `DISCORD_ENABLED=true` und den vollständigen Token in der lokalen `.env`.
- Deine User-ID in `DISCORD_ALLOWED_USER_IDS`.
- Bei Nachrichten im Server: korrekte Kanal-ID, Bot-Zugriff auf den Kanal und aktivierter Message
  Content Intent im Developer Portal.
- Bei Reminder-DMs: `NOTIFY_CHANNEL=discord` und eine gültige erste ID in der Allowlist.
