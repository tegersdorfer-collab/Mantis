"""Adaptive Trainingsplan-Generierung: pure Validierung + LLM-Orchestrierung."""
import logging
from datetime import date, datetime

log = logging.getLogger("mantis.plan")

DEFAULT_PLAN = {
    "lower": [
        {"name": "Squat", "weight": 100, "reps": 5, "sets": 4, "rpe": 8},
        {"name": "Romanian Deadlift", "weight": 80, "reps": 8, "sets": 3, "rpe": 7},
        {"name": "Leg Press", "weight": 140, "reps": 10, "sets": 3, "rpe": 8},
        {"name": "Leg Curl", "weight": 50, "reps": 12, "sets": 3, "rpe": 8},
        {"name": "Calf Raise", "weight": 60, "reps": 15, "sets": 4, "rpe": 9},
    ],
    "upper": [
        {"name": "Bench Press", "weight": 80, "reps": 6, "sets": 4, "rpe": 8},
        {"name": "Overhead Press", "weight": 50, "reps": 8, "sets": 3, "rpe": 7},
        {"name": "Barbell Row", "weight": 70, "reps": 8, "sets": 4, "rpe": 7},
        {"name": "Dumbbell Curl", "weight": 16, "reps": 10, "sets": 3, "rpe": 8},
        {"name": "Tricep Pushdown", "weight": 35, "reps": 12, "sets": 3, "rpe": 8},
        {"name": "Lateral Raise", "weight": 10, "reps": 15, "sets": 3, "rpe": 9},
    ],
}


def _clamp_int(v, lo: int, hi: int, default: int) -> int:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _clean_exercise_list(items) -> list:
    """Säubert eine Übungsliste: nur Einträge mit Namen, sets/reps gekappt."""
    if not isinstance(items, list):
        return []
    cleaned = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = (it.get("name") or "").strip()
        if not name:
            continue
        ex = {"name": name,
              "sets": _clamp_int(it.get("sets"), 1, 6, 3),
              "reps": _clamp_int(it.get("reps"), 1, 30, 8)}
        w = it.get("weight")
        try:
            if w is not None:
                ex["weight"] = float(w)
        except (TypeError, ValueError):
            pass
        rpe = it.get("rpe")
        if rpe is not None:
            ex["rpe"] = _clamp_int(rpe, 1, 10, 7)
        cleaned.append(ex)
    return cleaned


def normalize_plan(raw) -> dict | None:
    """Validiert LLM-JSON zu {lowerA,lowerB,upperA,upperB}. None wenn ungültig.
    Pflicht: lowerA + upperA nicht-leer. Fehlt eine B-Variante → B = A."""
    if not isinstance(raw, dict):
        return None
    lower_a = _clean_exercise_list(raw.get("lowerA"))
    upper_a = _clean_exercise_list(raw.get("upperA"))
    if not lower_a or not upper_a:
        return None
    lower_b = _clean_exercise_list(raw.get("lowerB")) or lower_a
    upper_b = _clean_exercise_list(raw.get("upperB")) or upper_a
    return {"lowerA": lower_a, "lowerB": lower_b, "upperA": upper_a, "upperB": upper_b}


def pick_variant(slot_count: int) -> str:
    """A bei gerader Anzahl bisheriger Slot-Sessions, sonst B (wechselt jede Runde)."""
    return "A" if slot_count % 2 == 0 else "B"


def needs_regen(plan: dict | None, today: date) -> bool:
    """True wenn kein Plan vorhanden oder der aktive Plan ≥42 Tage alt ist."""
    if not plan:
        return True
    created = plan.get("created_at")
    if isinstance(created, datetime):
        d = created.date()
    elif isinstance(created, date):
        d = created
    elif created:
        d = date.fromisoformat(str(created)[:10])
    else:
        return True
    return (today - d).days >= 42


def build_prompt(profile: dict, last_exercises: list[str], muscle_volume: dict) -> str:
    avoid = ", ".join(last_exercises) if last_exercises else "—"
    vol = ", ".join(f"{k}:{v}" for k, v in (muscle_volume or {}).items() if v)
    return (
        "Du bist ein erfahrener Strength-Coach und Personal Trainer. Erstelle einen "
        "durchdachten, sicheren 6-Wochen-Trainingsplan für einen Split mit zwei Krafttagen: "
        "LOWER (Beine, Hüfte, unterer Rücken, Core) und UPPER (Brust, Rücken, Schultern, Arme). "
        "Joggen ist separat und NICHT Teil des Plans.\n\n"
        f"Profil:\n- Ziel: {profile.get('goal')}\n- Equipment: {profile.get('equipment')}\n"
        f"- Erfahrung: {profile.get('experience')}\n"
        f"- Hinweise/Verletzungen: {profile.get('notes') or 'keine'}\n\n"
        f"Bisheriges Volumen je Muskel (30 Tage): {vol or 'wenig Daten'}\n"
        f"Übungen des letzten Blocks (VARIIERE, möglichst nicht wiederholen): {avoid}\n\n"
        "Erstelle JE ZWEI Varianten pro Krafttag (A und B), die sich klar unterscheiden — "
        "Timo wechselt jede Runde zwischen A und B für Abwechslung.\n"
        "Regeln pro Tag-Variante:\n"
        "- GENAU 6 Übungen. Jede der vier Listen (lowerA, lowerB, upperA, upperB) MUSS 6 Übungen haben.\n"
        "- Reihenfolge: schwerer Haupt-Compound → zweiter Compound → 2–3 Akzessorisch/Isolation → "
        "1 vernachlässigter Muskel.\n"
        "- Nutze GÄNGIGE, KURZE Übungsnamen ohne Klammer-Zusätze oder Kommentare "
        "(z.B. 'Beinpresse', 'Bankdrücken', 'Wadenheben', 'Nackenheben', 'Unterarm-Curl', 'Crunches').\n"
        "- Decke über UPPER-A und UPPER-B zusammen auch UNTERARME, NACKEN und HINTERE SCHULTER ab; "
        "über LOWER-A und LOWER-B zusammen auch WADEN und BAUCH/CORE.\n"
        "- Schemata passend zum Ziel (Hypertrophie: meist 3–4 Sätze × 6–12 Wdh, Hauptübung 4–6). "
        "Realistische Startgewichte in kg, passend zu Equipment und Erfahrung.\n\n"
        "Antworte AUSSCHLIESSLICH mit JSON in genau diesem Schema (jede Liste 6 Einträge):\n"
        '{"lowerA":[{"name":"...","weight":100,"reps":5,"sets":4,"rpe":8}],'
        '"lowerB":[...],"upperA":[...],"upperB":[...]}'
    )


async def generate_and_save(chat_llm, bg_llm=None) -> dict | None:
    """Generiert einen Plan über Chat- und Background-Cloud-Ketten, validiert, speichert.
    Gibt den gespeicherten Plan zurück oder None (dann bleibt der alte Plan aktiv)."""
    from domains import fitness
    from core.jsonutil import extract_json

    profile = fitness.get_training_profile()
    last = fitness.active_plan()
    last_ex: list[str] = []
    if last and isinstance(last.get("plan_json"), dict):
        for v in last["plan_json"].values():
            if isinstance(v, list):
                last_ex += [e.get("name") for e in v if isinstance(e, dict) and e.get("name")]
    muscle = fitness.muscle_volume(30)
    prompt = build_prompt(profile, last_ex, muscle)

    plan = None
    for llm in (chat_llm, bg_llm):
        if not llm:
            continue
        try:
            txt = await llm.chat(messages=[{"role": "user", "content": prompt}],
                                 temperature=0.4, max_tokens=2500, format="json")
            plan = normalize_plan(extract_json(txt, default=None))
            if plan:
                break
        except Exception:
            log.exception("Plan-LLM fehlgeschlagen, versuche Fallback")
            plan = None

    if not plan:
        log.warning("Plan-Generierung lieferte keinen gültigen Plan — alter Plan bleibt aktiv")
        return None

    seen = set()
    for key in ("lowerA", "lowerB", "upperA", "upperB"):
        for ex in plan[key]:
            if ex["name"] not in seen:
                seen.add(ex["name"])
                fitness.ensure_exercise(ex["name"])
    fitness.save_training_plan(name="Mantis-Block", goal=profile.get("goal", "muscle"),
                               weeks=6, plan=plan)
    log.info("Neuer A/B-Trainingsplan generiert und gespeichert")
    return plan
