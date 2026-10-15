# Spec: Gym-Tracker v2 – Plan, Übungen, Verlauf

Stand 2026-10-15. Baut auf `docs/specs/2026-10-14-gym-tracker-web.md` (v1) auf. Auftrag Timo
(15.10.): „gym App weiter ausbauen … zu nem gewissen Standard bringen“, Vorbild ist sein
AlphaProgression-Startbildschirm (Wochenleiste Mo–So, Plan „New plan“ mit Woche 8, Tage
Lower A / Upper A / Lower B / Upper B mit Übungsvorschau, Links „All plans“, „Exercises“).
Was er nutzt (14.10.): schnelles Abhaken mit vorausgefüllten Werten, Plan/Split, Pausentimer,
Verlauf. Progressionsvorschläge nutzt er nicht → weiterhin keine.

## Datenlage
- `exercises` (221, mit `rep_min/rep_max`, `weight_increment_kg`, `dumbbell_pair`, `muscle`),
  `exercise_aliases` (deutsche/englische Namen aus AP), `workouts` (261, `title` wie
  „Upper A · Day 1 · Week 2 · Sommer“), `workout_sets`.
- Letzter AP-Stand: Upper A, Lower A, Upper B, Lower B mit jeweils 6–10 Übungen.

## Umfang v2
1. **Plan-Modell** (neue Tabellen `gym_plans`, `gym_plan_days`, `gym_plan_exercises`): Plan mit
   Name, Startdatum, aktiv-Flag; geordnete Tage; je Tag geordnete Übungen mit Zielsätzen (Default 3)
   und Wdh.-Bereich (Default aus `exercises.rep_min/max`). Genau ein aktiver Plan.
   **Seed:** Migration/Skript erzeugt den Plan „Upper/Lower“ aus der AP-Historie: Tagesnamen aus
   `workouts.title` (Teil vor „·“; nur Lower A/Upper A/Lower B/Upper B), Übungen und Reihenfolge
   aus dem jeweils jüngsten Workout dieses Tags (Aliase auf kanonische Übung abgebildet,
   Ausdauer/Cardio ausgelassen), Zielsätze = Median der Arbeitssätze dort. Idempotent.
2. **Startbildschirm wie AP:** Wochenleiste Mo–So der laufenden Woche, Tage mit Training markiert,
   heute hervorgehoben; Planname + „Woche N“ (seit Planstart); Liste der Plantage mit Nummer und
   Übungsanzahl; **nächster Tag** in der Rotation (nach dem zuletzt trainierten Plantag)
   hervorgehoben. Offene Session → „Fortsetzen“ oben.
3. **Training aus Plantag starten:** Session mit Titel „<Tag> · Woche N“, Übungen des Tags in
   Reihenfolge; je Übung Zielsätze vorausgefüllt mit den Arbeitssätzen des letzten Trainings dieser
   Übung (wie v1), sonst leer. Während des Trainings: Übung überspringen, Übung ad hoc hinzufügen
   (Suche), Reihenfolge ändern, Satz hinzufügen/löschen, Notiz je Übung. Pausentimer wie v1,
   Standard je Übung aus Setting/Exercise. Freies Training ohne Plan bleibt möglich.
4. **Übungen:** Liste aller Übungen mit Suche (Name + Aliase), Muskelgruppe; Detail mit Verlauf
   (letzte Trainings, bester Satz, geschätztes 1RM nach Epley je Training als einfache
   Liniengrafik ohne externe Bibliothek); eigene Übung anlegen (Name, Muskel, Gerät, Wdh.-Bereich).
5. **Plan bearbeiten:** Tage umbenennen/hinzufügen/entfernen/sortieren, Übungen je Tag
   hinzufügen/entfernen/sortieren, Zielsätze und Wdh.-Bereich ändern. Neuer Plan anlegen,
   aktiven Plan wechseln („All plans“).
6. **Verlauf:** Liste vergangener Trainings (Datum, Titel, Dauer, Sätze, Volumen), Detail mit allen
   Sätzen; bearbeiten/löschen wie v1-Korrekturen.

## Nicht in v2
Live-Coach, Progressionsvorschläge, Supersätze, Bilder/Videos zu Übungen, Import neuer AP-Daten.

## Technik und Regeln
- Vanilla-JS ohne Build wie v1, mobil zuerst, Dark-Mode im AP-Stil (schwarz, Akzent Blau),
  große Touch-Ziele, Tab-Leiste unten (Training, Verlauf, Übungen, Plan).
- Alle neuen `/api/*` hinter dem bestehenden `mantis_gym`-Cookie und Origin-Prüfung; Offline-Queue
  und `client_id`-Idempotenz bleiben für Sätze; Plan-Änderungen online (Fehler sichtbar melden).
- Lokale Entwürfe offener Satzzeilen (Fix `fix/gym-reload`) bleiben kompatibel.
- Tests gegen `mantis_test`, keine echten Gesundheitsdaten in Fixtures (synthetische Übungen/Sätze).
- Abnahme: Seed auf Produktionsdaten trocken prüfen (nur lesen) und Ergebnis (Tage + Übungen)
  berichten, bevor er läuft.
