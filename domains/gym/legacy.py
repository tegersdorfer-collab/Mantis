"""Alias-Zuordnung auf die englischen Bestandsübungen aus dem AlphaProgression-Import.

Der kanonische Name ist immer der vorhandene Bestandsname; deutsche Namen und
Abkürzungen sind Aliasse darauf. ``equipment=None`` heißt unbekannt: die Übung
bekommt dann keine geratene Gewichtsstufe. Reine Daten, kein DB-Zugriff.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LegacyExercise:
    name: str
    equipment: str | None
    exercise_type: str
    aliases: tuple[str, ...]
    load_mode: str | None = None
    display_name: str | None = None


def _ex(name, equipment, exercise_type, *aliases, load_mode=None, display_name=None):
    return LegacyExercise(name, equipment, exercise_type, aliases, load_mode, display_name)


# Aliasse dürfen nicht mit KH/LH/SZ/Kabel/Maschine beginnen: Das Parser-Präfix
# wird vor der Aliassuche abgetrennt.
LEGACY_EXERCISES: tuple[LegacyExercise, ...] = (
    _ex("Bench Press", "barbell", "compound",
        "Bank", "Bankdrücken", "BD", "Bench", "Flachbank", "Flachbankdrücken",
        display_name="Bankdrücken"),
    _ex("Incline Bench Press", None, "compound",
        "Schrägbank", "Schrägbankdrücken", "Incline", "Incline Bench", "Incline Press"),
    _ex("Lateral Raises", "dumbbell", "isolation",
        "Seitheben", "Seitenheben", "Lateral Raise", "Side Raises"),
    _ex("Curls", None, "isolation",
        "Curls", "Curl", "Bizepscurls", "Bizepscurl", "Biceps Curl", "Bicep Curl", "Bizeps"),
    _ex("Hammer Curls", "dumbbell", "isolation",
        "Hammercurls", "Hammercurl", "Hammer Curl"),
    _ex("Reverse Curls", None, "isolation", "Reverse Curl", "Umgekehrte Curls"),
    _ex("Triceps Pushdowns", "machine", "isolation",
        "Trizepsdrücken", "Trizeps drücken", "Pushdowns", "Pushdown",
        "Trizeps Pushdowns", "Tricep Pushdown"),
    _ex("Triceps Pushdowns with Rope", "machine", "isolation",
        "Pushdowns Seil", "Trizepsdrücken Seil", "Rope Pushdowns"),
    _ex("Overhead Triceps Extensions", None, "isolation",
        "Überkopf Trizeps", "Trizeps Überkopf", "Trizeps über Kopf", "Overhead Extensions"),
    _ex("Overhead Triceps Extensions with Rope", "machine", "isolation",
        "Überkopf Trizeps Seil", "Trizeps Überkopf Seil"),
    _ex("Skull Crushers", "ez_bar", "isolation",
        "Skullcrusher", "Skull Crusher", "Stirndrücken", "French Press"),
    _ex("Triceps Kickbacks", None, "isolation", "Kickbacks", "Trizeps Kickbacks"),
    _ex("Butterfly with Wide Grip", "machine", "isolation",
        "Butterfly", "Butterfly weit", "Pec Deck"),
    _ex("Reverse Butterfly with Wide Grip", "machine", "isolation",
        "Reverse Butterfly", "Butterfly Reverse", "Reverse Butterfly weit",
        "Butterfly Reverse weit"),
    _ex("Reverse Flys", None, "isolation", "Reverse Fly"),
    _ex("High Cable Flys", "machine", "isolation", "High Cable Fly", "Cable Flys hoch"),
    _ex("Low Cable Flys", "machine", "isolation", "Low Cable Fly", "Cable Flys tief"),
    _ex("Seated Shoulder Press", None, "compound",
        "Schulterdrücken sitzend", "Schulterdrücken im Sitzen", "Seated OHP"),
    _ex("Shoulder Press", None, "compound",
        "Schulterdrücken", "OHP", "Overhead Press", display_name="Schulterdrücken"),
    _ex("Standing Shoulder Press", None, "compound",
        "Schulterdrücken stehend", "Standing OHP"),
    _ex("Arnold Press", "dumbbell", "compound", "Arnold"),
    _ex("Dips", None, "compound", "Dip", "Barrenstütz"),
    _ex("Chest Dips", None, "compound", "Brustdips"),
    _ex("Pull-Ups with Wide Overhand Grip", None, "compound",
        "Klimmzüge", "Klimmzug", "Pull-up", "Pull-ups", "Pullups", "Pull Up", load_mode="added"),
    _ex("Pull-Ups with Close Neutral Grip", None, "compound",
        "Klimmzüge eng", "Klimmzug eng", load_mode="added"),
    _ex("Face Pulls with Rope", "machine", "isolation", "Face Pulls", "Face Pull", "Facepulls"),
    _ex("Leg Press", "machine", "compound", "Beinpresse"),
    _ex("Leg Extensions", "machine", "isolation", "Beinstrecker", "Beinstreckung", "Leg Extension"),
    _ex("Seated Leg Curls", "machine", "isolation",
        "Beincurls sitzend", "Beinbeuger sitzend", "Seated Leg Curl"),
    _ex("Lying Leg Curls", "machine", "isolation",
        "Beincurls liegend", "Beinbeuger liegend", "Lying Leg Curl"),
    _ex("Seated Calf Raises", "machine", "isolation",
        "Wadenheben sitzend", "Waden sitzend", "Seated Calf Raise"),
    _ex("Standing Calf Raises", "machine", "isolation",
        "Wadenheben stehend", "Waden stehend", "Standing Calf Raise"),
    _ex("Calf Raises on Leg Press", "machine", "isolation", "Wadenheben Beinpresse"),
    _ex("Kneeling Crunches", "machine", "isolation", "Crunches", "Crunch"),
    _ex("Crunch Machine", "machine", "isolation", "Crunchmaschine", "Bauchmaschine"),
    _ex("Hanging Leg Raises", None, "isolation",
        "Beinheben hängend", "Hängendes Beinheben", "Hanging Leg Raise"),
    _ex("Shrugs", None, "isolation", "Shrug", "Schulterheben"),
    _ex("Bent-Over Rows", "barbell", "compound",
        "Rudern vorgebeugt", "Vorgebeugtes Rudern", "Bent Over Rows"),
    _ex("Squats", "barbell", "compound", "Kniebeuge", "Kniebeugen", "Squat"),
    _ex("Front Squats", "barbell", "compound", "Frontkniebeuge", "Front Squat"),
    _ex("Hack Squats", "machine", "compound", "Hackenschmidt", "Hack Squat", "Hacksquat"),
    _ex("Bulgarian Split Squats", None, "compound",
        "Bulgarian", "Bulgarian Split Squat", "Bulgarische Kniebeuge"),
    _ex("Split Squats", None, "compound", "Split Squat"),
    _ex("Walking Lunges", None, "compound", "Ausfallschritte", "Walking Lunge"),
    _ex("Deadlifts", "barbell", "compound", "Kreuzheben", "Deadlift"),
    _ex("Romanian Deadlifts", "barbell", "compound",
        "Rumänisches Kreuzheben", "RDL", "Romanian Deadlift"),
    _ex("Hip Thrusts", "barbell", "compound", "Hip Thrust", "Hüftheben"),
    _ex("Lat Pulldowns with Wide Overhand Grip", "machine", "compound",
        "Latzug", "Latziehen", "Lat Pulldown", "Lat Pulldowns", "Latzug weit",
        "Latzug weit mit Obergriff"),
    _ex("Lat Pulldowns with Close Neutral Grip", "machine", "compound",
        "Latzug eng", "Latzug eng Neutralgriff"),
    _ex("Chest Press", "machine", "compound", "Brustpresse"),
    _ex("Hip Adduction", "machine", "isolation", "Adduktoren", "Adduktorenmaschine"),
    _ex("Hip Abduction", "machine", "isolation", "Abduktoren", "Abduktorenmaschine"),
    _ex("Preacher Curls", None, "isolation", "Preacher Curl", "Scottcurls", "Scott Curls"),
    _ex("Incline Curls", "dumbbell", "isolation", "Incline Curl", "Schrägbankcurls"),
    _ex("One-Arm Lateral Raises", "dumbbell", "isolation", "Seitheben einarmig"),
    _ex("Front Raises", None, "isolation", "Frontheben", "Front Raise"),
    _ex("Russian Twists", None, "isolation", "Russian Twist"),
)

LEGACY_BY_NAME = {item.name.casefold(): item for item in LEGACY_EXERCISES}
LEGACY_BY_ALIAS = {
    value.casefold(): item
    for item in LEGACY_EXERCISES
    for value in (item.name, *item.aliases)
}


def legacy_exercise(name: str | None) -> LegacyExercise | None:
    return LEGACY_BY_NAME.get(str(name or "").strip().casefold())


def legacy_exercise_for_alias(name: str | None) -> LegacyExercise | None:
    """Gibt die Legacy-Übung für ihren kanonischen oder deutschen Namen zurück."""
    return LEGACY_BY_ALIAS.get(str(name or "").strip().casefold())
