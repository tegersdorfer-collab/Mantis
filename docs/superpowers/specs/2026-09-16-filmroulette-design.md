# Filmroulette – Design-Spezifikation

## Ziel

Filmroulette ist eine eigenständige Webapp für eine persönliche Filmliste. Sie
läuft getrennt von Mantis, verwendet eine eigene SQLite-Datenbank und ist im
Tailscale-Netz des Benutzers erreichbar.

Die App nimmt Filme über einen Titel oder einen IMDb-Link auf, ermittelt für
Deutschland die verfügbaren Anbieter, vergibt eine Hauptkategorie und bietet
ein mehrstufiges Glücksrad für ungesehene Filme. Gesehene Filme können mit
einer Ganzzahl von 1 bis 100 bewertet werden.

## Festgelegte Produktentscheidungen

- Der Markt ist fest auf Deutschland (`DE`) eingestellt.
- Die IMDb-TT-ID ist der eindeutige fachliche Schlüssel eines Films.
- Die vier Hauptkategorien sind:
  1. Prime Video enthalten
  2. Netflix enthalten
  3. Disney+ enthalten
  4. Andere
- Wenn ein Film in mehreren Abos enthalten ist, gilt die Reihenfolge
  `Prime Video > Netflix > Disney+`.
- Nur echte Flatrate-/Abo-Verfügbarkeit zählt für die ersten drei Kategorien.
  Leihe, Kauf und alle übrigen Fälle werden als Andere geführt.
- Ein Film wird im Glücksrad nur einmal berücksichtigt, unabhängig davon, wie
  viele Anbieter ihn führen.
- Die Bewertung ist optional, ganzzahlig und liegt zwischen 1 und 100.
- Bewerten darf man nur Filme, die als gesehen markiert wurden.
- Der Gewinner des Glücksrads wird automatisch als gesehen markiert und erhält
  den aktuellen Zeitpunkt.

## Nicht Bestandteil der ersten Version

- Serien und Episoden
- Benutzerkonten oder Mehrbenutzerverwaltung
- öffentliche Bereitstellung außerhalb des Tailscale-Netzes
- automatische Hintergrundaktualisierung aller Filme ohne Benutzeraktion
- Import aus externen Watchlists

## Nutzerfluss

### Film hinzufügen

Die Oberfläche bietet ein gemeinsames Hinzufügen-Modul:

1. Der Benutzer gibt einen Filmtitel oder eine IMDb-URL ein.
2. Bei einer IMDb-URL wird die TT-ID extrahiert und direkt aufgelöst.
3. Bei einem Titel liefert eine Suchansicht mehrere Treffer mit Titel, Jahr,
   Poster und IMDb-ID. Der Benutzer bestätigt genau einen Treffer.
4. Der Backend-Katalogadapter lädt Metadaten und deutsche Anbieter-Daten.
5. Der Film wird anhand seiner IMDb-ID dedupliziert gespeichert.
6. Die Oberfläche zeigt die Hauptkategorie, alle erkannten Anbieter, den
   Prüfzeitpunkt und einen Hinweis, falls die Anbieterprüfung nicht sicher
   abgeschlossen werden konnte.

Eine erneute Aufnahme derselben IMDb-ID erzeugt keinen zweiten Datensatz,
sondern gibt den bereits vorhandenen Film zurück.

### Filmübersicht

Die Hauptansicht enthält:

- Kennzahlen für alle Filme, ungesehene Filme und gesehene Filme
- die vier Kategorie-Filter
- Filter für gesehen/ungesehen
- eine Freitextsuche über Titel und Originaltitel
- Sortierung nach Titel, Erscheinungsjahr, Bewertung, Gesehen-am und letzter
  Anbieterprüfung
- eine Tabelle bzw. auf kleinen Bildschirmen Kartenliste mit Titel, Jahr,
  Kategorie, Anbieter-Badges, gesehenem Status, Bewertungswert und Aktionen

Die Bewertung ist in der Liste nur bei gesehenen Filmen editierbar. Eine
ungültige Bewertung wird vor dem Speichern abgewiesen und verständlich
angezeigt.

### Glücksrad

Der Glücksrad-Flow besteht aus zwei Phasen:

1. Das erste Rad zieht bis zu fünf verschiedene Filme aus allen ungesehenen
   Filmen. Bei weniger als fünf ungesehenen Filmen werden alle vorhandenen
   Filme verwendet. Nach jedem Ziehen wird der gewählte Titel aus dem Pool
   entfernt, damit kein Kandidat doppelt erscheint.
2. Das zweite Rad startet mit diesen maximal fünf Kandidaten. Die ersten drei
   Drehungen scheiden jeweils einen Titel aus. Danach bleiben zwei Titel übrig;
   die vierte Drehung entscheidet den Gewinner zwischen diesen beiden.
3. Nach der Entscheidung zeigt die App den Titel, die Kategorie und die
   Zielaktion. Prime-Video- und Netflix-Filme öffnen einen gespeicherten oder
   berechneten Anbieterlink. Disney+- und Andere-Filme öffnen die IMDb-Seite.
4. Die App speichert den Gewinner unmittelbar serverseitig als gesehen und
   schreibt den aktuellen Zeitstempel in `watched_at`.

Wenn kein ungesehener Film existiert, zeigt die App einen leeren Zustand statt
eines leeren oder fehlerhaften Rads. Bei weniger als zwei Kandidaten wird der
letzte vorhandene Titel direkt als Gewinner angezeigt; unnötige
Ausscheidungsdrehungen entfallen.

## Datenquelle und Identität

IMDb ist die fachliche Identität, aber nicht die einzige Datenquelle. Der
Katalogadapter verwendet TMDB, um eine IMDb-ID auf einen Film aufzulösen und
Metadaten sowie deutsche Watch-Provider-Daten abzurufen. Die
Watch-Provider-Antwort wird als Rohdatenkopie gespeichert, damit eine
Kategorieentscheidung nachvollziehbar bleibt und später neu berechnet werden
kann. Die Anbieter-Daten sind regional und zeitlich veränderlich; jeder
Datensatz erhält deshalb `availability_checked_at`.

Die Integration kapselt alle externen Aufrufe hinter einem
`CatalogProvider`-Interface. Die restliche Anwendung kennt weder die konkrete
HTTP-Bibliothek noch TMDB-Response-Formate. Für Tests wird ein deterministischer
Fake-Provider verwendet.

Für Anbieterlinks gilt folgende Priorität:

1. ein vom Katalogadapter gelieferter direkter Link,
2. ein anbieterspezifischer Suchlink mit dem Filmtitel,
3. bei Disney+ und Andere immer die gespeicherte IMDb-URL.

Die App verspricht keinen direkten Anbieterfilm-Link, wenn die Quelle nur
einen allgemeinen Kataloglink liefert; in diesem Fall wird der sichere
Suchlink verwendet.

## Kategorisierung

Der Adapter normalisiert Anbieter über eine zentrale Zuordnung aus stabiler
Provider-ID und normalisiertem Namen. Berücksichtigt werden ausschließlich
Provider aus dem Flatrate-/Abo-Feld. Kauf- und Leihfelder werden nur für die
Anzeige der verfügbaren Geschäftsmodelle gespeichert, beeinflussen aber nie
die drei Abo-Kategorien.

Die reine, deterministische Fachfunktion lautet sinngemäß:

```text
wenn Prime-Video-Flatrate vorhanden: Prime Video enthalten
sonst wenn Netflix-Flatrate vorhanden: Netflix enthalten
sonst wenn Disney+-Flatrate vorhanden: Disney+ enthalten
sonst: Andere
```

Ein externer Fehler wird nicht als bestätigte Anbieterinformation ausgegeben.
Bei einer ersten Aufnahme bleibt die sichtbare Fallback-Kategorie Andere,
aber `availability_status=unknown` zeigt an, dass eine erneute Prüfung nötig
ist. Bei einer späteren fehlgeschlagenen Aktualisierung bleiben die letzte
bestätigte Kategorie und Rohdaten erhalten; nur der Status und der
Prüfzeitpunkt werden aktualisiert.

## Architektur

Die eigenständige Anwendung besteht aus einem kleinen Python-Projekt neben
Mantis:

```text
filmroulette/
├── app/
│   ├── main.py          # FastAPI-App und Startkonfiguration
│   ├── config.py        # Umgebungsvariablen und sichere Host-Prüfung
│   ├── db.py            # SQLite-Verbindung, Migrationen, Repository-Helfer
│   ├── models.py        # interne Dataclasses und API-Schemata
│   ├── catalog.py       # CatalogProvider, TMDB-Adapter, Linkaufbau
│   ├── movies.py        # Hinzufügen, Filmliste, Bewertung, gesehen-Status
│   ├── roulette.py      # Kandidaten- und Ausscheidungslogik
│   ├── routes.py        # REST-Endpunkte
│   └── static/
│       ├── index.html
│       ├── app.js
│       └── styles.css
├── tests/
├── requirements.txt
├── requirements-test.txt
├── .env.example
└── README.md
```

Die Webapp wird durch FastAPI ausgeliefert. Die Oberfläche bleibt bewusst
frameworkarm: Vanilla-JS rendert die Ansichten, ein SVG- oder Canvas-Rad
visualisiert die vom Backend entschiedenen Ziehungen. SQLite liegt in einem
konfigurierten Datenpfad außerhalb des Quellcodes.

### Datenmodell

Die zentrale Tabelle wird als SQLite-Tabelle `movies` angelegt:

```sql
CREATE TABLE movies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    imdb_id TEXT NOT NULL UNIQUE,
    tmdb_id INTEGER,
    title TEXT NOT NULL,
    original_title TEXT,
    release_year INTEGER,
    overview TEXT,
    poster_path TEXT,
    imdb_url TEXT NOT NULL,
    primary_category TEXT NOT NULL
        CHECK (primary_category IN ('prime', 'netflix', 'disney', 'other')),
    providers_json TEXT NOT NULL DEFAULT '[]',
    availability_json TEXT NOT NULL DEFAULT '{}',
    availability_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (availability_status IN ('verified', 'unknown', 'error')),
    availability_checked_at TEXT,
    watched INTEGER NOT NULL DEFAULT 0 CHECK (watched IN (0, 1)),
    watched_at TEXT,
    rating INTEGER CHECK (rating IS NULL OR (rating BETWEEN 1 AND 100)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_movies_category ON movies(primary_category);
CREATE INDEX idx_movies_watched ON movies(watched);
CREATE INDEX idx_movies_rating ON movies(rating);
```

`providers_json` enthält die normalisierten Anzeige- und Linkdaten; in
`availability_json` liegt die unveränderte bzw. ausreichend vollständige
Antwort des Katalogadapters. JSON wird beim Lesen defensiv validiert, damit
eine beschädigte externe Antwort die gesamte Liste nicht unlesbar macht.

### REST-Schnittstelle

Die API wird unter `/api` ausgeliefert:

- `GET /api/movies?query=&category=&watched=&sort=title&direction=asc`
  liefert die gefilterte und sortierte Übersicht. Sortierspalten werden gegen
  eine feste Allowlist geprüft.
- `GET /api/movies/search?q=...` liefert maximal zehn Titelkandidaten aus dem
  externen Katalog, ohne sie zu speichern.
- `POST /api/movies` nimmt `{ "title": "..." }` nach bestätigter Suche oder
  `{ "imdb_url": "https://www.imdb.com/title/tt.../" }` entgegen. Die Antwort
  enthält den gespeicherten Film.
- `POST /api/movies/{id}/refresh` aktualisiert Metadaten und Anbieterstatus
  eines einzelnen Films.
- `PATCH /api/movies/{id}/watched` nimmt `{ "watched": true|false }` entgegen.
  Beim Wechsel auf true wird `watched_at` gesetzt; beim manuellen Zurücksetzen
  werden `watched_at` und `rating` gelöscht.
- `PATCH /api/movies/{id}/rating` nimmt `{ "rating": 1..100 }` entgegen und
  liefert 400, wenn der Film noch ungesehen ist.
- `POST /api/roulette/shortlist` liefert maximal fünf eindeutige ungesehene
  Filme. Der Endpoint akzeptiert optional `{ "count": 5 }`, begrenzt den Wert
  aber serverseitig auf 1 bis 5.
- `POST /api/roulette/eliminate` nimmt `{ "movie_ids": [...] }` entgegen,
  wählt serverseitig genau einen noch aktiven Kandidaten aus und liefert den
  ausgeschiedenen Film plus die verbleibenden IDs.
- `POST /api/roulette/decide` nimmt `{ "movie_id": ..., "candidate_ids": [...] }`
  entgegen, validiert, dass der Film noch ungesehen und Kandidat ist, markiert
  ihn atomar als gesehen und liefert die Zielaktion.

Antwortfehler sind konsistent: 400 für ungültige Eingaben, 404 für unbekannte
IDs, 409 für Duplikate oder einen inzwischen gesehenen Roulette-Kandidaten und
502/503 für nicht verfügbare externe Katalogdienste bzw. fehlende
Konfiguration. Fehlende Anbieterprüfung bei einer Aufnahme darf die
Metadatenaufnahme nicht zerstören; sie wird als `unknown` sichtbar gemacht.

## Tailscale-Betrieb und Sicherheit

Die Standardkonfiguration bindet nur an `127.0.0.1`. Für Zugriff im Tailnet
wird eine explizite Tailscale-Adresse über `FILMROULETTE_HOST` konfiguriert.
Akzeptiert werden Loopback- oder Tailscale-Adressen; `0.0.0.0`, `::` und
gewöhnliche LAN-/öffentliche Adressen werden abgelehnt. Der Port ist mit
`FILMROULETTE_PORT` konfigurierbar und standardmäßig 7780.

TMDB-Zugangsdaten werden ausschließlich aus `TMDB_API_TOKEN` gelesen und
bleiben in `.env`, die durch `.gitignore` ausgeschlossen wird. Die App
zeigt keine Zugangsdaten in Fehlermeldungen oder API-Antworten. Das Tailscale-
Netz ist die Zugriffsschranke der ersten Version; ein zusätzlicher App-Login
ist nicht Teil des MVP.

## Fehlerbehandlung und Betriebsverhalten

- Unbekannte oder nicht auflösbare IMDb-IDs erzeugen eine verständliche
  404-/422-nahe API-Antwort und keinen halbfertigen Film.
- TMDB-Timeouts und Rate-Limits werden mit kurzer, begrenzter Retry-Logik
  behandelt; danach bleibt der Film bei einem Refresh unverändert und erhält
  einen Fehlerstatus.
- Eine kaputte Poster-URL darf die Tabelle und das Glücksrad nicht blockieren;
  die Oberfläche zeigt einen neutralen Platzhalter.
- Der Gewinner-Endpoint prüft den Status in derselben SQLite-Transaktion, in
  der `watched` und `watched_at` geschrieben werden. Doppelte Klicks können so
  keinen zweiten Gewinner erzeugen.
- Die Anbieterprüfung kann für einzelne Filme per Refresh erneut ausgelöst
  werden; es gibt keine ungefragten externen Schreibaktionen.

## Teststrategie und Abnahmekriterien

Die Tests laufen ohne Netzwerk und ohne TMDB-Schlüssel gegen eine temporäre
SQLite-Datenbank.

Es gibt mindestens Tests für:

- Extraktion gültiger IMDb-TT-IDs aus URL und Roh-ID sowie Ablehnung fremder
  URLs und leerer Eingaben
- Deduplication über `imdb_id`
- Kategoriepriorität Prime vor Netflix vor Disney und Andere als Fallback
- Ausschluss von Leihe/Kauf aus den Abo-Kategorien
- Speicherung und Aktualisierung von `availability_checked_at` und
  Rohdatenstatus
- Filtern, Sortieren und Paginierungs-/Limit-Verhalten der Filmliste
- Ablehnung von Bewertung 0, 101, Dezimalwerten und Bewertung ungesehener Filme
- Löschen der Bewertung beim manuellen Zurücksetzen auf ungesehen
- fünf eindeutige Shortlist-Kandidaten bzw. alle Kandidaten bei kleinerem Pool
- drei gültige Ausscheidungsdrehs und vierter Entscheidungsdreh
- atomare Gewinner-Entscheidung, Zeitstempel und Wiederholungs-409
- Anbieterlink/Fallback-Logik
- Ablehnung öffentlicher Bind-Adressen

Die Umsetzung gilt als abgeschlossen, wenn ein Benutzer ohne manuelle
Datenbankänderung einen Film hinzufügen, seine Kategorie sehen, die Liste
sortieren/filtern, das Glücksrad bis zur Entscheidung durchlaufen und den
Gewinner anschließend mit einer Bewertung zwischen 1 und 100 versehen kann.
Die App muss dabei über die konfigurierte Tailscale-Adresse erreichbar sein,
ohne Mantis zu importieren oder dessen Datenbank zu verwenden.
