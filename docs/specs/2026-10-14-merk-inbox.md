# Spec: Merk-Inbox über Discord

Stand: 2026-10-14 · Schritt 1 von 2 · nur Analyse und Entwurf

## Ziel

Timo kann unterwegs im Discord-DM einen Link, eine Idee oder einen Screenshot teilen und
später natürlichsprachlich wiederfinden. Die Erfassung muss schnell sein und darf einen
normalen Chat nicht versehentlich in einen Speichervorgang verwandeln. Es gibt keine
proaktiven Nachrichten.

Erfolg für v1: Eine einzelne Sache wird mit Quelle, brauchbarem Titel, Kategorie und Tags
gespeichert, sofort kurz bestätigt und später per Suche gefunden, als erledigt markiert,
korrigiert oder gelöscht. Die Daten bleiben in Mantis' eigener Datenbank und gelangen nicht
in Timos Claude-/Codex-Agentengedächtnis.

## Bestandsanalyse

### Speicherort und vorhandene Brain-Tools

- Mantis' Second Brain ist PostgreSQL, nicht Timos Obsidian-Agentengedächtnis:
  brain_notes und brain_links werden in core/db.py:504-527 angelegt. Es gibt in den
  geprüften Brain-Tool-Pfaden keinen Dateizugriff und keinen Vault-Pfad. Die Erwähnung
  „Kategorien (wie Obsidian-Ordner)“ in domains/second_brain.py:4 ist ein Strukturvergleich,
  keine Obsidian-Integration. Insbesondere wird ~/SecondBrain/Brain nicht beschrieben.
- brain_save registriert sich in core/skills/knowledge.py:40-53 und ruft
  domains/second_brain.py:add_note auf. Der Eintrag landet als Titel, Markdown-Inhalt,
  Kategorie, Tags und optionalem Embedding in brain_notes
  (domains/second_brain.py:110-145).
- brain_inbox in core/skills/knowledge.py:63-70 speichert über
  brain_tool_inbox_add in domains/second_brain.py:457-462 in brain_notes mit Kategorie
  inbox. Das allgemeine Inbox-Sorting kann Einträge später umsortieren
  (domains/second_brain.py:399-436); es kennt aber nicht die für diese Funktion nötigen
  Kategorien und Statuswerte.
- brain_search delegiert in core/skills/knowledge.py:55-61 an
  brain_tool_search (domains/second_brain.py:448-454). search_notes unterstützt optionale
  pgvector-Suche (domains/second_brain.py:296-332), aber brain_tool_search übergibt kein
  embedding_fn. Das bestehende Agent-Tool sucht damit aktuell per ILIKE in Titel, Inhalt
  und Tags; eine echte semantische Suche ist dort nicht aktiv.
- brain_daily_log hängt Einträge an eine Daily Note in brain_notes an
  (core/skills/knowledge.py:72-79; domains/second_brain.py:464-469).
- save_quote und add_thought_to_quote speichern Zitate und Gedanken ebenfalls in
  brain_notes, Kategorie quote (core/skills/knowledge.py:195-218;
  domains/second_brain.py:351-368).
- fetch_and_summarize_url nutzt tools/url_handler.fetch, ruft anschließend ein LLM für eine
  Zusammenfassung auf und speichert standardmäßig als brain_notes-Ressource
  (core/skills/knowledge.py:145-193). Das wäre für eine schnelle Merk-Inbox unnötig teuer
  und würde Inhalte in den allgemeinen Brain-Bestand mischen.
- add_research_query legt wöchentliche Suchthemen als brain_notes-Ressourcen an
  (core/skills/knowledge.py:134-143).
- import_kindle_highlights importiert Highlights über add_quote als brain_notes-Zitate
  (core/skills/knowledge.py:81-132).

### Links, Anhänge und Vision

- tools/url_handler.py:58-100 erkennt YouTube, TikTok und Instagram anhand der Domain und
  routet sie an yt-dlp. tools/url_handler.py:102-155 holt dabei Metadaten ohne Video-Download.
  Ein tatsächlicher Transkriptabruf ist dort nicht implementiert: Die Untertitel-Schleife
  bricht nur nach dem Auffinden einer Sprache ab (Zeilen 122-128).
- Artikel laufen über trafilatura, mit HTTPX-/HTML-Fallback
  (tools/url_handler.py:158-231). Dabei wird auch Seiteninhalt extrahiert. Für die Inbox
  genügen zunächst Titel, Beschreibung, Autor und Plattform; eine schlanke Metadaten-Funktion
  sollte daher keinen vollständigen Artikeltext sammeln, wenn die Metadaten verfügbar sind.
- fetch_and_summarize_url ist keine Metadaten-only-Funktion: Sie hängt einen LLM-Aufruf an
  den Fetch an und schreibt das Ergebnis in brain_notes
  (core/skills/knowledge.py:155-193).
- Discord ignoriert Anhänge nicht still, sondern lehnt jede Nachricht mit Anhang ab, bevor
  sie an den gemeinsamen Handler geht (communication/discord.py:325-343). Der aktuelle
  Test erwartet diese Antwort (tests/test_discord_channel.py:261-273). Der Handler nutzt
  bereits die Allowlist und DM-/Kanal-Intents (communication/discord.py:278-286, 312-323).
- IncomingMessage enthält Text, Absender, Zeit, Rohobjekt und Kanal, aber kein eigenes
  Attachment-Feld (communication/base.py:11-20). Für Bilder muss der Discord-Eingang
  ausgewählte Bildanhänge aus message.attachments lesen, Größe und MIME-Typ prüfen und
  die Bytes oder ein geprüftes Analyse-Ergebnis in den Turn weiterreichen.
- Die gemeinsame Vision-Funktion sendet Bilder an die bildfähigen Stufen der Cloud-Kette
  (`core/vision.py`); auch Telegram-Fotos und Inbox-Bilder nutzen diesen Pfad
  (communication/telegram.py:326-380). Die Chat-Backends übergeben derzeit Textinhalte
  (core/backends/openrouter.py:36-75, core/backends/claude.py:30-67,
  core/backends/chatgpt_plan.py:176-209). Ein Vision-Aufruf über die Chat-Kette benötigt
  daher neue multimodale Provider-Übersetzung; das kann der vorhandene Bildpfad nicht
  erledigen.

### Ablageempfehlung

Für v1 eine eigene Tabelle merk_inbox_items in Mantis' PostgreSQL verwenden, nicht
brain_notes und nicht ~/SecondBrain/Brain/Inbox. brain_notes ist Mantis' allgemeines
Second Brain; seine Suche, Sortierung und Graph-Ansicht mischen Wissensnotizen verschiedener
Zwecke. Eine eigene Tabelle trennt die Lebenszyklen und erlaubt strukturierte Filter,
Erledigt-Status, Quellen-Metadaten und eine Inbox-eigene semantische Suche. Die Ablage bleibt
lokal bei Mantis und außerhalb des Claude-/Codex-Agentenindex. Es gibt keinen Vault-Sync.

Mindestfelder: id, title, user_note, source_kind (link, image, text oder mixed),
source_urls, platform, source_metadata (JSONB), image_description, category, tags,
status (open oder done), embedding, created_at, updated_at, completed_at,
source_message_id und source_message_ids. source_message_id bleibt der eindeutige
Erstbezug für Gateway-Retrys; source_message_ids enthält alle Share- und
Bestätigungsnachrichten-IDs, auf die eine Discord-Antwort zeigen kann. Die IDs der
Bestätigungsnachrichten werden zusätzlich separat markiert, damit Lösch-Replies nur darauf
autorisieren. Pro Discord-Nachricht entsteht höchstens ein Eintrag;
mehrere Links bleiben gemeinsam in source_urls, Metadaten werden für den ersten Link geholt.
Das Bild selbst wird in v1 nicht gespeichert; gespeichert werden nur die vom Vision-Modell
erkannten Informationen. Löschen entfernt den Datensatz samt Embedding endgültig.

## Erfassung

### Nachrichten als Merk-Vorgang oder normaler Chat

Der Discord-Adapter prüft weiter Absender und DM-/Kanalregeln und übergibt erlaubte
Nachrichten an den gemeinsamen Message-Handler. Die Intent-Entscheidung fällt dort vor der
bisherigen normalen Persistenz und dem allgemeinen Agenten-Turn; so kann die User-Zeile
direkt mit dem richtigen Mess-Tag gespeichert werden.

1. Ein einzelner Link ohne Frage/Arbeitsauftrag wird gespeichert.
2. „Merk dir das“, „für später“ oder eine vergleichbare explizite Speicheranweisung speichert
   Link, Text oder Bild. Ein Bild ohne Text gilt als bewusster Share und wird gespeichert.
3. Ein Link mit einer Frage oder einem Arbeitsauftrag („Was ist das?“, „Fass das zusammen“)
   bleibt ein normaler Agent-Chat. Das verhindert, dass eine Recherchefrage als Merk-Vorgang
   verschluckt wird.
4. Ein Text ohne Link, Bild oder klare Speicheranweisung bleibt normaler Chat.
5. Wenn mehrere Interpretationen plausibel sind, nicht speichern. Der Agent soll normal
   antworten oder kurz nachfragen, statt stillschweigend Daten anzulegen.

Ein erfolgreicher Merk-Vorgang nutzt einen direkten, begrenzten Pfad statt eines allgemeinen
ReAct-Tool-Rundlaufs: Nachricht klassifizieren, zunächst mit Eingabetext, URLs und
Fallback-Titel einen Datensatz idempotent speichern, dann Metadaten/Vision mit kurzem
Timeout best-effort ergänzen und die knappe Bestätigung senden. Der Commit wartet nicht auf
Netzwerk oder Modell. URL- oder Vision-Fehler dürfen den Speichervorgang nicht verhindern.
Wenn Anreicherung vor Ablauf des Zeitbudgets gelingt, wird der Datensatz vor der Bestätigung
aktualisiert; andernfalls wird der Fallback bestätigt. Es gibt keine spätere ungefragte
Statusnachricht.

### Link-Erkennung und Metadaten

- URLs aus Text und Discord-Share-Sheet mit tools/url_handler.extract_urls erkennen und
  um abschließende Satzzeichen bereinigen. Nur HTTP(S) zulassen. Redirects, Antwortgröße
  und Zeit begrenzen; weder Login-Cookies noch persönliche Zugangsdaten an Seiten senden.
- Für YouTube, TikTok und Instagram Metadaten über yt-dlp ohne Medien-Download versuchen.
  Titel, Beschreibung, Creator/Kanal, Plattform, Vorschaubild-URL und verfügbare
  Veröffentlichungsdaten übernehmen. Kein Transkript und keine Zusammenfassung für die
  bloße Ablage laden.
- Öffentliche YouTube-Links liefern häufig Titel und Kanal; private, altersbeschränkte,
  gelöschte oder rate-limitierte Videos können ohne Login scheitern. In dem Fall bleibt der
  Original-Link mit dem vom Host abgeleiteten Plattformnamen erhalten.
- TikTok-Kurzlinks und Instagram-Reels/Posts laufen über denselben yt-dlp-Pfad. Login-
  Beschränkungen, private Posts und kurzlebige Links können Metadaten verhindern. Das ist
  kein Grund, die Nachricht zu verwerfen.
- Artikel und Blogs: Titel/Beschreibung/Autor aus HTML-Metadaten bzw. trafilatura holen.
  Keine Zusammenfassung und keinen Volltext speichern, solange Timo das nicht später
  ausdrücklich anfordert.
- Fehlen Metadaten, den vom Nutzer beigefügten Text als Titel/Kontext verwenden; andernfalls
  einen lesbaren Fallback wie „YouTube-Link“ plus gekürzte URL. Erfassung darf nicht von
  erfolgreichem Fetch abhängen.
- Die Metadaten-Funktion soll keine Zusammenfassung per LLM erzeugen. Plattform und
  verfügbare Seitendaten werden deterministisch übernommen; ein optionaler Klassifikator
  erhält nur den für Kategorie und Tags nötigen Inhalt.

### Bilder und Screenshots

Nur Bildanhänge (gängige Bild-MIME-Typen) werden in v1 unterstützt. Discord lädt sie nur
für den erlaubten Absender herunter; maximal vier Bilder mit jeweils höchstens 7,5 MB
Rohdaten pro Nachricht, damit jedes Bild nach Base64-Kodierung unter 10 MB bleibt.
Nicht-Bild-Anhänge und Sprachnachrichten behalten eine kurze verständliche Ablehnung.

Der Anhang und optionale Caption werden an eine multimodale Vision-Stufe der Chat-Kette
übergeben. Sie erkennt OCR-Text, sichtbare Titel/Marken/Orte sowie eine kurze neutrale
Beschreibung, die für spätere Suche taugt. Das ist ein Cloud-Aufruf. Timo hat entschieden,
dass der Chat den vollen Kontext an die Cloud senden darf; für diesen begrenzten Vorgang
werden nur Bild, Caption und Analyseauftrag übertragen, nicht der gesamte Gesprächsverlauf.
Wenn keine Cloud-Vision-Stufe verfügbar ist, bleibt der Bildinhalt unanalysiert; die
Erfassung soll dann Caption bzw. „Bild aus Discord“ speichern und die Einschränkung in der
Bestätigung nennen. Keine Speicherung der Bildbytes oder Discord-CDN-URL.

## Kategorie, Tags und Bestätigung

Jeder Eintrag erhält genau eine Kategorie:

- anschauen (Anzeige: Anschauen) – Video, Clip, Trailer oder sonstiger visueller Inhalt
- lesen (Anzeige: Lesen) – Artikel, Blog, längerer Text oder Buch
- film-serie (Anzeige: Film) – explizit als Film oder Serie erkannter Titel
- kaufen (Anzeige: Kaufen) – Produkt, Shop-Angebot oder Kaufidee
- idee (Anzeige: Idee) – eigener Gedanke oder Inspiration, z. B. Balkon-Idee
- ort (Anzeige: Ort) – Ort, Restaurant oder Reiseziel
- zitat (Anzeige: Zitat) – als Zitat erkannter oder ausdrücklich so markierter Text
- sonstiges (Anzeige: Sonstiges) – verlässlicher Fallback

Erst Regeln aus explizitem Nutzertext und Plattform/Seitentyp anwenden, dann bei unklarem
Inhalt einen auf diese Kategorien beschränkten Klassifikator verwenden. Bei Modellfehler,
Timeout oder ungültigem Ergebnis deterministisch zurückfallen und sonstiges wählen. Keine
Kategorie darf den Speichervorgang blockieren.

Tags ergänzen den Gegenstand und Themen, nicht die Quelle: etwa scifi, balkon oder kochen.
Plattform ist ein eigenes Metadatum; optional kann sie zusätzlich als System-Tag angezeigt
werden. Tags werden normalisiert und in der Antwort mit # dargestellt. Höchstens zwei
inhaltliche Tags in der Sofortbestätigung.

Antwort direkt nach erfolgreichem DB-Commit, kurz und ohne Zusammenfassung:

    📥 gemerkt: „Arrival“ · Film · #scifi

Ohne ausreichende Metadaten einen ehrlichen Fallback-Titel verwenden. Einträge mit
Metadatenfehler werden trotzdem bestätigt. Der Zielpfad soll ohne allgemeinen Agenten-
Rundlauf antworten; ein sinnvoller Zielwert ist p95 unter 5 Sekunden inklusive kurzem
Metadatenversuch. Der Datenbank-Commit darf nicht auf Fetch oder Vision warten.

## Abfragen und Aktionen

### Natürliche Suche

Beispiele:

- „Was wollte ich mir noch anschauen?“ → offene Einträge, Kategorie anschauen
- „Welche Filme hab ich mir gemerkt?“ → offene Einträge, Kategorie film-serie
- „Was war die Idee mit dem Balkon?“ → semantischer Suchtext Balkon, bevorzugt Kategorie idee
- „Zeig mir Links von letzter Woche“ → source_urls vorhanden, Zeitfenster der letzten Woche

Der Agent übersetzt die Anfrage in ein enges Suchobjekt: query, category, created_after,
created_before, status und has_url. SQL-Filter schränken die Kandidaten zuerst ein;
Keyword- und Embedding-Suche liefern anschließend hybride Treffer. Bei Filtern ohne
Suchtext genügt strukturierte SQL-Suche. Standard ist status open, sofern die Frage nicht
nach erledigten oder allen Einträgen fragt. Zeitfenster verwenden Europe/Berlin.

Die semantische Suche darf ausschließlich merk_inbox_items durchsuchen, nicht brain_notes,
memories oder den KG. Embeddings bleiben in der Inbox-Tabelle und nutzen die bestehende
Embedding-Infrastruktur; die Dimension muss zum vorhandenen Vektorvertrag passen.
Ergebnisse enthalten Titel, Kategorie, Datum, Status und Quelle. Bei mehreren Treffern
werden wenige passende Einträge mit Links angezeigt; wenn nichts passt, ehrlich „nichts
gefunden“ melden.

### Erledigt, Korrektur und Löschen

- „Hab ich gesehen“ markiert einen eindeutig referenzierten offenen Eintrag als done und
  setzt completed_at. Ohne eindeutigen Bezug wird nachgefragt; bei genau einem plausiblen
  jüngsten offenen Eintrag darf dieser gemeint sein.
- „Korrigiere …“ kann Titel, Notiztext, Kategorie, Tags oder Link ändern. Nur genannte
  Felder überschreiben. Bei mehreren passenden Einträgen oder unklarem Änderungsziel zuerst
  nachfragen.
- „Lösch …“ löscht den eindeutig benannten Eintrag endgültig. Eine mehrdeutige Löschanfrage
  braucht Rückfrage; keine Sammellöschung per vager Formulierung.
- Ein kurzer stabiler Eintragsbezug (ID oder Antwort-Thread auf die Bestätigung) soll
  Änderungen auch bei gleichnamigen Titeln ermöglichen.

## Datenschutz und Grenzen

- Die Inbox ist ein eigener PostgreSQL-Bereich in Mantis. Keine Markdown-Dateien,
  Frontmatter, automatische Links oder Syncs in ~/SecondBrain/Brain; keine Ablage in
  brain_notes.
- Gespeichert werden nur notwendige Metadaten, Nutzernotiz, URL, Bildbeschreibung/OCR,
  Kategorie, Tags, Status und Embedding. Originalbildbytes werden nach Analyse verworfen.
- Link-Fetches sind best effort und ohne private Cookies. Keine Videos herunterladen,
  keine geschützten Inhalte umgehen und keine URL-Parameter mit Zugangstoken in Logs
  schreiben.
- Vision ist explizit Cloud-Verarbeitung. Metadaten/Kategorie können ebenfalls an die
  konfigurierte Chat-Kette gehen, falls ein Modell nötig ist. Keine Hintergrundanalyse
  privater Inhalte; in der Cloud nur das für diesen Turn benötigte Material senden.
- Logs enthalten Vorgangstyp, Dauer und Ergebnisstatus, nicht Bildbytes, Volltext oder
  vollständige URLs. Kein proaktives Nachfassen, tägliches Sortieren oder Reminder.

## Messbarkeit

chat_messages besitzt bereits ein JSONB-Feld meta (core/db.py:353-360), und die
Persistenzfunktion kann Meta-Werte annehmen (core/message_handler.py:346-357). Bei jedem
erkannten Inbox-Turn wird {"use_case":"inbox"} in meta geschrieben. Das gilt für Nutzer-
und Assistant-Nachricht, damit Erfassung, Suche, Erledigt, Korrektur und Löschung über Kanal
und Tag auswertbar sind. Bestehende Metadaten wie tools und model bleiben erhalten.

Mindestmetriken: Inbox-Turns pro Tag, Erfassungen, spätere Suchabfragen, erledigte und
gelöschte Einträge sowie Erfassungsfehler. Keine Inhaltsdaten in der Metrik. Normale
Discord-Chats ohne Inbox-Intent erhalten kein use_case=inbox.

## Tests für Schritt 2

- Ablage-Isolation: neue Einträge, Suche und Löschung berühren merk_inbox_items, nie
  brain_notes oder Dateien im Obsidian-Vault.
- Erfassungsentscheidung: nackter Link und Bild ohne Caption speichern; explizites
  „merk dir“ speichert; Link mit Frage/Arbeitsauftrag läuft in den Agenten; normaler Text
  bleibt Chat; mehrdeutige Fälle erzeugen keinen stillen Eintrag.
- Idempotenz: dieselbe Discord source_message_id erzeugt keinen zweiten Datensatz.
- URL-Metadaten: YouTube, TikTok, Instagram und Artikel; erfolgreiche Metadaten,
  Redirect/Timeout, privater oder ungültiger Link; kein LLM-Zusammenfassungsaufruf und kein
  Video-Download.
- Bild: erlaubte Bildtypen werden heruntergeladen, an Vision weitergegeben und Bytes danach
  verworfen; zu große/nicht unterstützte Anhänge werden kontrolliert behandelt; Vision-
  Fehler verhindert die Capture nicht.
- Kategorie/Tags: alle acht Kategorien, Fallback bei Modellfehler, normalisierte Tags und
  kurze Bestätigung mit ehrlichem Fallback-Titel.
- Suche: Keyword- und semantische Treffer, Kategorie-/Zeit-/Status-/URL-Filter,
  Europe/Berlin-Zeitgrenzen und leere Treffer; keine Ergebnisse aus anderen Speichern.
- Aktionen: Erledigt, Korrektur einzelner Felder, eindeutige und mehrdeutige Löschung,
  Verweis über ID/Discord-Antwort.
- Messbarkeit: use_case=inbox auf Inbox-Turns, unverändertes Meta bei normalen Chats.
- Discord: DM-Allowlist bleibt wirksam; Bildanhänge erreichen den neuen Pfad, andere
  Anhänge erhalten eine knappe verständliche Antwort.

## Offene Fragen an Timo

1. Soll bei Bild-Merkungen wirklich nur OCR/Bildbeschreibung erhalten bleiben, oder soll
   das Originalbild optional abrufbar sein? Vorschlag für v1: nicht speichern.
2. Wenn derselbe Link zweimal geteilt wird: als zwei Merker behalten oder den offenen
   Eintrag aktualisieren? Vorschlag: getrennt behalten; Discord-Retrys werden unabhängig
   davon über source_message_id dedupliziert.
3. Soll die Kategorie film-serie schon v1 Filme/Serien-Titel zuverlässig von allgemeinen
   Clips unterscheiden, oder genügt eine gute Klassifikation aus Nachricht und Metadaten?
   Vorschlag: zunächst Klassifikation, keine Anbindung an Filmroulette.
4. Soll später ein expliziter Export in Mantis' allgemeines Second Brain möglich sein?
   Vorschlag: v1 bleibt vollständig getrennt; kein automatischer Export.
5. Soll eine Nachricht mit mehreren Bildern und Links zu einem gemeinsamen Eintrag
   zusammengefasst werden? Vorschlag: ja, ein Eintrag pro Nachricht; alle URLs erhalten,
   mehrere Bilder gemeinsam analysieren, innerhalb eines Größenlimits.

## Entscheidungen (Claude, 14.10.2026 nachts; Timo bestätigt/ändert morgens)
1. Originalbild in v1 nicht speichern, nur erkannte Inhalte.
2. Abweichend vom Vorschlag: Gibt es zum selben (normalisierten) Link schon einen **offenen** Eintrag, keinen Duplikat-Eintrag anlegen, sondern antworten „📌 Hast du dir schon am 12.10. gemerkt“ und dessen updated_at aktualisieren; eine neue Notiz des Nutzers wird an user_note angehängt. Erledigte Einträge → neuer Eintrag.
3. Klassifikation aus Nachricht + Metadaten, keine Filmroulette-Anbindung in v1.
4. Kein Export in v1.
5. Ja, ein Eintrag pro Nachricht.
6. **Konservative Erfassung:** Eine Nachricht wird nur erfasst, wenn sie mit einem
   ausdrücklichen Merk-Auslöser beginnt (z. B. „merk dir“, „merke“, „für später“,
   „später anschauen/lesen“, „inbox:“ oder „📥“; jeweils mit Wortgrenzen und ohne
   Verneinung wie „merk dir das nicht“), oder wenn bei einem Link der Text außerhalb
   der URLs leer ist bzw. nur eine kurze Notiz mit höchstens ungefähr zwölf Wörtern ohne
   Fragezeichen, Fragewort oder Arbeitsauftrag enthält, oder wenn ein Bild ohne Caption
   geteilt wird. Bei Bildern mit Text gilt ebenfalls nur ein ausdrücklicher Merk-Auslöser.
   Fragen und Aufträge bleiben Agent-Turns, auch wenn sie einen Link enthalten. Abfragen
   wie „Zeig mir meine gespeicherten Links“ sind keine Erfassung.
7. **Löschungen laufen nie über den LLM-Agenten oder die Tool-Auswahl.** Der deterministische
   Fast-Path akzeptiert nur eine vollständige Nachricht nach
   `^(lösch(e)?|entfern(e)?)\s+(den\s+)?(merker|eintrag)?\s*#?(\d+)\s*[.!]?$`, oder einen
   Discord-Reply auf eine gespeicherte Merker-Bestätigungsnachricht, dessen kompletter Text
   nur `lösch(e) (das|den)?` und optional `bitte` enthält. Jede erkannte Löschung fragt zuerst
   `🗑️ „Titel“ löschen? ja/nein`; gelöscht wird nur bei der direkt folgenden eingehenden
   Nachricht `ja` desselben Absenders. Jede andere nächste Nachricht verwirft die Bestätigung.
   Das Agent-Tool `inbox_delete`
   ist entfernt. IDs für Korrektur und Erledigt-Markierung werden ausschließlich aus Text
   außerhalb von URLs gelesen; URL-Fragmente wie `#42` zählen nicht.
8. **Anreicherung schützt Nutzereingaben.** Kategorie und Tags werden nur aktualisiert, wenn
   ihr DB-Wert noch dem Capture-Snapshot entspricht. `category_source` ist `auto` oder `user`;
   eine eindeutige Metadaten-Klassifikation darf eine automatische provisorische Kategorie
   ersetzen, solange der Nutzer die Kategorie nicht korrigiert hat. Metadaten-Timeouts
   beenden immer die gesamte yt-dlp-Prozessgruppe, auch wenn der Leader bereits beendet ist.
   Für yt-dlp werden YouTube-, TikTok- und Instagram-Hosts normalisiert und exakt gegen die
   Allowlist geprüft; dazu gehören `www.youtube.com`, `m.youtube.com`,
   `music.youtube.com`, `www.tiktok.com` und `www.instagram.com`.
Zusätzlich: Embeddings mit dem bereits im Repo genutzten lokalen Embedding-Weg (kein neues großes Modell; RAM-Regel: nie zwei lokale Modelle > 4 GB gleichzeitig). yt-dlp 2026.06.09 ist installiert; Metadaten-Abruf mit hartem Timeout (≤ 8 s), die Bestätigung an Timo darf nicht auf langsame Metadaten warten (sofort „📥 gemerkt“, Titel/Kategorie werden nachgetragen und bei Abfragen verwendet; optional die Bestätigung per Edit aktualisieren).
