# Forge launchd-Job

Installation (ausserhalb des Worktrees, Timos Schritt):

```
launchctl bootout gui/$(id -u)/com.mantis.forge 2>/dev/null
cp forge/launchd/com.mantis.forge.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mantis.forge.plist
```

Verwaister Worktree nach MERGED (Absturz zwischen Merge und Aufräumen):
`git worktree remove --force ~/Mantis-forge/task-N` im Hauptrepo, danach
`git branch -d forge/task-N`.

## Forge-Bot (Nachtrag 3a)

Der Telegram-Bot läuft 24/7 als eigener Agent. Voraussetzungen:
`FORGE_BOT_TOKEN` in `~/.config/ai-keys.env`, `TELEGRAM_CHAT_ID` in `.env`.
Ohne eins von beiden endet er mit Exit 2 und einer Zeile in
`/tmp/mantis_forge_bot_err.log`; launchd wartet dann 60 s (`ThrottleInterval`).
Exit 3 = Datenbank nicht erreichbar (launchd versucht es nach 60 s erneut).

```
launchctl bootout gui/$(id -u)/com.mantis.forge-bot 2>/dev/null
cp forge/launchd/com.mantis.forge-bot.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mantis.forge-bot.plist
```

Läuft er? `launchctl list | grep forge-bot` (PID in der ersten Spalte) und
`/status` im Chat mit t.me/AIMantisBot.

Vor einem manuellen `python3.14 -m forge.bot` (z. B. zum Debuggen) erst den
Agenten stoppen — `launchctl bootout gui/$(id -u)/com.mantis.forge-bot` —
sonst pollen zwei Prozesse gleichzeitig und Telegram antwortet mit 409
Conflict.
