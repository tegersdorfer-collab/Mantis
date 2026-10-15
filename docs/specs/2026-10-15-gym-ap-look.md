# Spec: Gym-App im AlphaProgression-Look

Stand 2026-10-15. Auftrag Timo: „optisch wirklich eine Kopie von alpha progression … nur für mich“.
Referenz-Screenshots (nicht im Repo, enthalten persönliche Werte): `~/Mantis/data/design/ap/`
(0_home, 1_verlauf, 2_profil, 3_uebungsdetail, 4_satz_eintragen). Grenze: Layout, Farben,
Typografie und Interaktion nachbauen; **keine** AP-Bilder, Videos, Logos, Trophäen-Grafiken
übernehmen oder laden. Statt Übungsfotos: runde Kacheln mit Muskelgruppen-Icon/Initialen.

## Design-Tokens (aus den Screenshots)
- Hintergrund App `#000000`, Karten/Sheets `#1C1C1E`, darin Gruppenflächen `#2C2C2E`,
  Pillen/Eingabefelder `#3A3A3C`. Trennlinien kaum sichtbar, keine Rahmen.
- Akzent Blau `#0A84FF` (Links, Titel der aktiven Übung, „Close“, „Share“, Wrench-Icons,
  „+“-Kreise, Primärbutton). Erfolg Grün `#30D158`/`#00C27A` (aktiver Haken-Button,
  Pausentimer-Pille). Text primär `#FFFFFF`, sekundär `#8E8E93`, tertiär `#636366`.
- Schrift: System (SF Pro / -apple-system), große fette Titel (Übungsname ~34 px bold,
  Screen-Titel „Training“ ~40 px heavy), Zahlen in Tabellen ~28 px regular, Labels
  Großbuchstaben grau (`# KG REPS 10RM`). Radien: Karten 22 px, Pillen 18–20 px, Buttons
  vollrund. Großzügiger Weißraum, 20 px Seitenrand.
- Tab-Leiste unten, 4 Icons ohne Text: Hantel (Training), Fläche/Chart (Verlauf),
  Person (Profil), Schloss (in AP Premium – bei uns: Einstellungen/Plan). Aktives Icon weiß,
  inaktive `#3A3A3C`-grau.

## Screens
1. **Start (0_home):** oben rechts blauer Link „Create“. Wochenleiste Mo–Su mit Kürzel
   (grau) und Datum, heute weiß fett. Zeile: Planname groß blau („New plan“) + runde
   „•••“-Pille, rechts „Week 8“ blau. Karte mit nummerierten Tagen (Kreis-Ziffer grau, Name
   weiß 22 px), rechts Pille mit 3 runden Übungs-Kacheln + „+N“. Darunter blaue Links
   „All plans“, „Exercises“, „Help“. Während eines Trainings: großer blauer Balken-Button
   über der Tab-Leiste „⚡ Lower A · 33 sec“ (laufende Trainingszeit) → öffnet Training.
2. **Training/Übung (4_satz_eintragen)** als Bottom-Sheet mit Griff oben:
   rechts oben blau „Lower A 🔧“; horizontale Reihe runder Übungs-Kacheln aller Übungen
   des Tages (aktuelle markiert, erledigte mit blauem Häkchen-Badge), Tippen wechselt.
   Übungsname groß blau + „•••“-Pille (Menü: überspringen, ersetzen, Notiz, entfernen).
   Untertitel grau „Machine ⇄  6 reps ✎“ (Gerät, Ziel-Wdh. editierbar).
   Pille „1 warm-up set“ (Aufwärmsätze ein-/ausblenden).
   Tabelle `#🔧  KG✎  REPS  10RM✎`: erledigte Sätze als Text mit grauem ✓ rechts;
   aktueller Satz: Nummer in Pille mit blauem Punkt, KG und REPS als graue Pillen
   (Tippen → Zahlenfeld), rechts großer grüner Haken-Button; künftige Sätze grau vorbefüllt.
   Spalte „10RM“ = auf Ziel-Wdh. umgerechnetes Gewicht (Epley-Inverse), editierbar oben.
   Darunter Karte „Tue, Sep 22nd · Lower A“ mit der Tabelle des letzten Trainings dieser Übung.
   Blauer Link „Help“. Unten mittig grüne Pille „⏱ 1:59“ = laufender Pausentimer
   (Tippen: ±30 s / überspringen). Für Zeitübungen Spalte „SECS“ statt REPS, kein 10RM.
3. **Verlauf (1_verlauf)** Sheet: „Close“ links, „Share“ rechts (blau, Share = Text export),
   Titel „Training“ groß, Untertitel „287 workouts“. Karte mit Chart (bei uns echt:
   Volumen/Woche oder Trainings pro Woche; Filter-Pillen „1mo 📅“ und „Workouts 🔧“).
   Darunter Karte: Liste Trainings (Tagname weiß, Datum grau „Thu, Oct 8th“), rechts Pille
   mit 3 Übungs-Kacheln mit blauem Häkchen-Badge + „+N“. Tippen → Trainingsdetail.
4. **Übungsdetail (3_uebungsdetail)** Sheet: „Close“, runde Kachel mittig, „Share“;
   Segment-Control „Info | Instructions | History“ (aktives Segment dunkle Pille).
   Name groß + „•••“. Karten: Equipment → Wert grau rechts; Muskeln (primär weiß, sekundär
   grau) mit Körper-Silhouette vorne/hinten als SVG, betroffene Muskeln weiß hervorgehoben;
   „Show all (n)“ blau; Type; Metrics („Weight + reps“), Weight „kg“; Bodyweight „0%“;
   „Similar (n)“ blau. Statt AP-Premium-Kasten zeigen wir die echte e1RM-Kurve.
   Instructions: eigener Freitext/Notizen (leer erlaubt). History: Trainings dieser Übung.
5. **Profil (2_profil):** „Settings“ rechts oben blau. Statt Trophäen-Grafik ein schlichtes
   eigenes Abzeichen (SVG, kein AP-Asset). Zwei Kacheln: „287 workouts“ (Icon gelb),
   „4w streak“ (Flammen-Icon gelb; Streak = Wochen in Folge mit ≥1 Training). Karte „Gym“.
   Karte Bodyweight / Body fat percentage mit Wert grau und blauem „+“-Kreis (Eingabe →
   Mantis-Gesundheitsdaten, falls Tabelle vorhanden, sonst eigene Tabelle). Links „Add
   measurement“, „Basic info“, „Help“. Aktiver-Training-Balken wie auf Start.

## Verhalten
- Sheets mit Slide-up-Animation, Wischen nach unten schließt. Haptik/`navigator.vibrate`
  beim Haken. Alle bestehenden Funktionen bleiben (Offline-Queue, Entwürfe, Plan-Editor).
- Barrierefreiheit: Buttons mit `aria-label`, Kontrast der Grautöne ≥ 4,5:1 für Text.
- Kein Build-Schritt, Vanilla-JS, alles lokal (keine CDN-Fonts).
