# God's Eye View für Mantis

Die Integration nutzt den Upstream-Commit
`a65d9d85f1faa06ae7df235d7fa8a29b026b7a5b` unter der MIT-Lizenz.
Quelle, Lizenz und Datenquellen-Bedingungen:
https://github.com/bilawalsidhu/gods-eye-view

`scripts/setup-gods-eye-view.sh` installiert den Upstream nach
`data/gods-eye-view/` (von Mantis-Git ignoriert), kopiert die schmale Mantis-
Browser-Bridge und erlaubt Einbettung ausschließlich aus dem lokalen
Mantis-Dashboard. Es werden keine Mantis-Zugangsdaten an GEV übergeben.

Der Sidecar-Prozess muss auf demselben Mac wie der Browser laufen und an
`127.0.0.1:4173` gebunden sein. Der Mantis-Backendprozess läuft auf Port 7779.
Der bestehende GEV-Server ist laut Upstream nicht für öffentlichen oder
ungeschützten LAN-Betrieb ausgelegt. Keine `HOST=0.0.0.0`-Konfiguration nutzen.

Start:

```sh
./scripts/setup-gods-eye-view.sh
cd data/gods-eye-view
npm run dev -- --host 127.0.0.1 --port 4173
```

Auf macOS kann GEV anschließend dauerhaft als lokaler LaunchAgent laufen:

```sh
./scripts/install-gods-eye-view-service.sh
```

Der Dienst startet bei Anmeldung und wird nach einem Absturz neu gestartet.
Er bindet fest an `127.0.0.1:4173`.

Danach Mantis lokal öffnen und **God's Eye View** in der Navigation wählen.
Optionale Provider-Schlüssel werden nur in GEV selbst eingerichtet; dessen
Anbieterbedingungen und Quoten gelten unabhängig von Mantis.
