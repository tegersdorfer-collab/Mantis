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
Verbindung; das Skript muss nur erneut laufen, wenn du den Zugriff im
COROS-Account entziehst.

## Region

Default ist die EU-Route. Liegt der Account woanders, in die `.env`:

```
COROS_MCP_URL=https://mcpus.coros.com/mcp
```

Verfügbar sind `mcpeu`, `mcpus` und `mcpcn`.

## Nachsehen, was das MCP liefert

```bash
python3 scripts/coros_probe.py
```

Schreibt Tool-Katalog und Beispiel-Antworten nach `tests/fixtures/coros/`.

## Wenn es klemmt

| Symptom | Ursache |
|---|---|
| `COROS nicht autorisiert` | Kein Token-File — `scripts/coros_auth.py` laufen lassen |
| `COROS-Refresh abgelehnt` | Zugriff im COROS-Account entzogen — neu autorisieren |
| Alle Tools 401 | Falsche Region, siehe oben |
| Tool meldet fehlende Parameter | `inputSchema` in `tests/fixtures/coros/_tools.json` ansehen |
