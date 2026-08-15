# COROS-MCP einrichten

Mantis holt Gesundheits- und Trainingsdaten direkt aus der COROS-Cloud über deren
offizielles MCP. Ersetzt den alten Weg über Apple Health und die BodyOS-App.

## Einmalig

```bash
cd ~/Mantis
python3 scripts/coros_auth.py
```

Öffnet den COROS-Login im Browser. Nach der Zustimmung liegt das Token in
`data/coros_token.json` (Rechte 0600, gitignored). Der Refresh-Token hält die
Verbindung; das Skript muss nur erneut laufen, wenn der Zugriff im COROS-Account
entzogen wurde, der Refresh-Token abgelaufen ist oder die Token-Datei nicht
lesbar ist.

## Region

Default ist die EU-Route. Liegt der Account woanders, in die `.env`:

```
COROS_MCP_URL=https://mcpus.coros.com/mcp
```

Verfügbar sind `mcpeu`, `mcpus` und `mcpcn`.

**Beim Wechseln der Region immer zuerst `data/coros_token.json` löschen.**
`oauth.access_token()` refresht gegen den `token_endpoint`, der *im Token selbst*
gespeichert ist — eine alte Datei redet also unbegrenzt weiter mit der alten
Region, egal was in `COROS_MCP_URL` steht. `scripts/coros_auth.py` erkennt einen
Issuer-Wechsel zwar und registriert dann neu, aber ohne gelöschte Token-Datei
versucht `access_token()` weiterhin zuerst den alten Refresh.

## Nachsehen, was das MCP liefert

```bash
python3 scripts/coros_probe.py
```

Schreibt Tool-Katalog und Beispiel-Antworten nach `data/coros_samples/`
(gitignored, da unter `data/`).

**Achtung, personenbezogene Daten:** `queryUserInfo` enthält Geburtsdatum, Größe
und Gewicht, `querySportRecords` die Startkoordinaten von Läufen (de facto die
Heimadresse). Die Dumps sind absichtlich außerhalb der Versionskontrolle — vor
jeder Verwendung außerhalb dieses Rechners von Hand durchsehen und redigieren.

## Wenn es klemmt

| Symptom | Ursache |
|---|---|
| `COROS nicht autorisiert` | Kein Token-File — `scripts/coros_auth.py` laufen lassen |
| `COROS-Refresh abgelehnt` | Zugriff entzogen, Refresh-Token abgelaufen oder Token-Datei unlesbar — neu autorisieren |
| Alle Tools 401 | Falsche Region, siehe oben (und `data/coros_token.json` löschen!) |
| Tool meldet fehlende Parameter | `inputSchema` in `data/coros_samples/_tools.json` ansehen |
