# Forge launchd-Job

Installation (ausserhalb des Worktrees, Timos Schritt):

```
launchctl bootout gui/$(id -u)/com.mantis.forge 2>/dev/null
cp forge/launchd/com.mantis.forge.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mantis.forge.plist
```
