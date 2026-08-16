"""COROS-Prosa → health_data-Spalten. Reine Funktionen, kein I/O.

Das COROS-MCP liefert keine strukturierten Daten: kein outputSchema, kein
structuredContent, nur LLM-erzeugten Text mit fester Zeilenstruktur. Dieses Modul
holt daraus die Zahlen.

Leitlinie ist Strenge. Ein fehlender Wert ist harmlos — health_scores meldet dann
ehrlich "zu wenig Daten". Ein falsch geparster Wert sieht aus wie eine Messung und
verschiebt die Baselines dauerhaft. Deshalb: exakte Muster, Plausibilitätsgrenzen,
und im Zweifel nichts.
"""
import logging
import re

log = logging.getLogger("mantis.coros")


class CorosFormatError(RuntimeError):
    """Die Antwort ist kein verwertbarer Datensatz (Fehlertext oder leer)."""


# COROS antwortet auf manche Aufrufe mit isError=false und einem Fehlertext im
# Inhalt. Diese Muster erkennen das, bevor irgendein Parser darüber läuft.
_ERROR_MARKERS = (
    "anomalies detected",
    "must be in yyyymmdd format",
    "invalid parameter",
    "no permission",
    "unauthorized",
    "coros api error",
    "out of range",
)


def check_response(text: str) -> str:
    """Text durchreichen — oder CorosFormatError, wenn es gar keine Daten sind."""
    if not text or not text.strip():
        raise CorosFormatError("Leere Antwort vom COROS-MCP")
    low = text.lower()
    for marker in _ERROR_MARKERS:
        if marker in low:
            raise CorosFormatError(f"COROS meldet einen Fehler statt Daten: {text[:160]}")
    return text


def parse_int(raw: str) -> int | None:
    """'12,500' → 12500. 'No data', Leerstring und Unsinn → None.

    Verlangt wohlgeformte Dreiergruppen nach jedem Komma. Ein abgeschnittener
    Stream (SSE-Antwort bricht mitten in der Zahl ab) darf nicht als Zahl mit
    weniger Stellen durchgehen — '1,0' ist keine 10 und keine 1, sondern gar
    nichts. Die Lookbehind/Lookahead-Guards verhindern, dass die Regex bei einem
    fehlgeschlagenen Dreiergruppen-Match einfach auf das führende Fragment
    zurückfällt (z.B. '1' aus '1,0') oder ein Fragment nach dem Komma als eigene
    Zahl aufgreift (z.B. '0' aus '1,0').
    """
    if raw is None:
        return None
    m = re.search(r"(?<![\d,])-?\d{1,3}(?:,\d{3})*(?![\d,])", raw)
    if not m:
        return None
    return int(m.group(0).replace(",", ""))


def parse_duration_h(raw: str) -> float | None:
    """'8h 10min' → 8.17, '59 min' → 0.98, '0 min' → 0.0. Sonst None.

    Auf zwei Nachkommastellen, wie die übrigen Stundenwerte in health_data.
    """
    if raw is None:
        return None
    m = re.search(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*min)?", raw)
    if m.group(1) is None and m.group(2) is None:
        return None
    hours = int(m.group(1) or 0) + int(m.group(2) or 0) / 60
    return round(hours * 100) / 100


# Plausibilitätsgrenzen (inklusive). Bewusst weit — sie sollen Parser-Ausrutscher
# abfangen (verklebte Zahlen, verrutschte Felder), keine ungewöhnlichen Tage.
LIMITS: dict[str, tuple[float, float]] = {
    "steps":            (0, 120_000),
    "active_calories":  (0, 20_000),
    "exercise_minutes": (0, 1_440),
    "stress_avg":       (0, 100),
    "sleep_duration":   (0, 24),
    "sleep_deep":       (0, 24),
    "sleep_core":       (0, 24),
    "sleep_rem":        (0, 24),
    "sleep_awake":      (0, 24),
    "resting_hr":       (20, 150),
    "hr_avg":           (20, 220),
    "hr_min":           (20, 220),
    "hr_max":           (20, 250),
    "hrv":              (1, 300),
    "vo2max":           (10, 100),
    "recovery_pct":     (0, 100),
    "sleep_score":      (0, 100),
    "training_load_short": (0, 2_000),
    "training_load_long":  (0, 2_000),
    "weight":           (20, 400),
}


def sane(col: str, value):
    """Wert zurückgeben, oder None wenn er außerhalb der Grenzen liegt."""
    if value is None:
        return None
    lo, hi = LIMITS.get(col, (float("-inf"), float("inf")))
    if lo <= value <= hi:
        return value
    log.warning(f"COROS: {col}={value} außerhalb [{lo}, {hi}] — verworfen")
    return None


def _put(target: dict, col: str, value) -> None:
    """Nur setzen, wenn ein plausibler Wert da ist. Kein None, keine 0 aus Versehen."""
    v = sane(col, value)
    if v is not None:
        target[col] = v


_DAY_RE = re.compile(r"^--- (\d{8}) ---$")


def parse_daily_health(text: str) -> dict[str, dict]:
    """queryDailyHealthData → {'2026-08-11': {'steps': …}, …}.

    Erwartetes Blockformat je Tag:

        --- 20260811 ---
        Steps: 12,500 | Calories: 1,200 kcal | Exercise: 25 min
        Stress: Avg 50
        Sleep Summary:
          Total: 6h 0min | Deep: 1h 0min | Light: 3h 30min | REM: 1h 15min | Awake: 15 min
          Sleep HR: Avg 60 bpm | Min 50 bpm | Max 85 bpm

    'Sleep HR' wird bewusst NICHT auf hr_avg/hr_min/hr_max abgebildet — das ist der
    Puls im Schlaf, nicht der des Tages. Die Tageswerte kommen aus queryAvgHeartRate.
    """
    check_response(text)
    out: dict[str, dict] = {}
    day: str | None = None

    for line in text.splitlines():
        stripped = line.strip()

        m = _DAY_RE.match(stripped)
        if m:
            raw = m.group(1)
            day = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
            out.setdefault(day, {})
            continue
        if day is None:
            continue

        fields = out[day]

        if stripped.startswith("Steps:"):
            for label, col in (("Steps", "steps"), ("Calories", "active_calories"),
                               ("Exercise", "exercise_minutes")):
                mm = re.search(rf"{label}:\s*([^|]+)", stripped)
                if mm:
                    _put(fields, col, parse_int(mm.group(1)))
        elif stripped.startswith("Stress:"):
            _put(fields, "stress_avg", parse_int(stripped.split("Avg", 1)[1])
                 if "Avg" in stripped else None)
        elif stripped.startswith("Total:"):
            for label, col in (("Total", "sleep_duration"), ("Deep", "sleep_deep"),
                               ("Light", "sleep_core"), ("REM", "sleep_rem"),
                               ("Awake", "sleep_awake")):
                mm = re.search(rf"\b{label}:\s*([^|]+)", stripped)
                if mm:
                    _put(fields, col, parse_duration_h(mm.group(1)))

    # Tage ohne einen einzigen erkannten Wert wieder rauswerfen — sonst schreibt der
    # Importer leere Zeilen und die Scores halten den Tag für erfasst.
    return {d: f for d, f in out.items() if f}


_ISO_DAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}):?$")


def parse_sleep(text: str) -> dict[str, dict]:
    """querySleepData → {'2026-08-10': {'sleep_score': 77}, …}.

    Nur der Sleep Score. Die Phasen liefert dieses Tool bloß als Prozentanteile;
    absolute Stunden kommen aus parse_daily_health, und zwei Quellen für dieselbe
    Spalte wären eine Fehlerquelle ohne Gewinn.
    """
    check_response(text)
    out: dict[str, dict] = {}
    day: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        m = _ISO_DAY_RE.match(stripped)
        if m:
            day = m.group(1)
            continue
        if day and stripped.startswith("Sleep Score:"):
            fields: dict = {}
            _put(fields, "sleep_score", parse_int(stripped.split(":", 1)[1]))
            if fields:
                out[day] = fields
    return out


def parse_sleep_hrv(text: str) -> dict[str, dict]:
    """querySleepHrv → {'2026-08-12': {'hrv': 71}, …}.

    Liest nur den Tagesabschnitt. Danach folgt eine Rohzeitreihe mit zehntausenden
    'timestamp=… hrv=…'-Zeilen; die wird übersprungen, weil sie keine Tagesmarker hat.
    """
    check_response(text)
    out: dict[str, dict] = {}
    day: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("timestamp="):
            day = None            # Rohzeitreihe erreicht — ab hier nichts mehr übernehmen
            continue
        m = _ISO_DAY_RE.match(stripped)
        if m:
            day = m.group(1)
            continue
        if day and stripped.startswith("HRV Avg:"):
            fields: dict = {}
            _put(fields, "hrv", parse_int(stripped.split(":", 1)[1]))
            if fields:
                out[day] = fields
            day = None            # 'Baseline:' darunter darf den Wert nicht ersetzen
    return out


def _parse_day_value_lines(text: str, pattern: re.Pattern, build) -> dict[str, dict]:
    """Gemeinsame Form für 'YYYY-MM-DD: …'-Zeilen: je Treffer ein Tag."""
    check_response(text)
    out: dict[str, dict] = {}
    for line in text.splitlines():
        m = pattern.match(line.strip())
        if not m:
            continue
        fields = build(m)
        if fields:
            out[m.group(1)] = fields
    return out


_RHR_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}):\s*(\d+)\s*bpm\b")


def parse_resting_hr(text: str) -> dict[str, dict]:
    """queryRestingHeartRate → {'2026-08-11': {'resting_hr': 47}, …}.

    Der Wert wird direkt hinterm Doppelpunkt verlangt ('NN bpm'). Ein Format wie
    'No data (last known 58 bpm)' darf NICHT matchen — sonst holt sich
    `parse_int` die erste Zahl irgendwo im Rest der Zeile, und ein alter,
    veralteter Wert sieht wie eine frische Messung aus.
    """
    def build(m):
        fields: dict = {}
        _put(fields, "resting_hr", parse_int(m.group(2)))
        return fields
    return _parse_day_value_lines(text, _RHR_LINE, build)


_AVG_HR_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}):\s*(\d+)\s*bpm(?:\s*\(Min:\s*([^,]+),\s*Max:\s*([^)]+)\))?$"
)


def parse_avg_hr(text: str) -> dict[str, dict]:
    """queryAvgHeartRate → {'2026-08-11': {'hr_avg': 85, 'hr_min': 52, 'hr_max': 140}, …}.

    Der Durchschnittswert wird wie `parse_resting_hr` direkt hinterm Doppelpunkt
    verlangt ('NN bpm') statt aus freiem Text vor der optionalen Min/Max-Klammer
    gezogen zu werden — sonst würde z.B. 'No data, last known 58 bpm' den alten
    Wert 58 als aktuellen Tagesdurchschnitt übernehmen. Min/Max bleiben durch die
    Klammer- bzw. Komma-Grenzen strukturell schon eindeutig begrenzt.
    """
    def build(m):
        fields: dict = {}
        _put(fields, "hr_avg", parse_int(m.group(2)))
        _put(fields, "hr_min", parse_int(m.group(3) or ""))
        _put(fields, "hr_max", parse_int(m.group(4) or ""))
        return fields
    return _parse_day_value_lines(text, _AVG_HR_LINE, build)


_PLAIN_DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})$")


def parse_training_load(text: str) -> dict[str, dict]:
    """queryTrainingLoadAssessment → kurz-/langfristige Last je Tag.

    Load Ratio und Comment werden bewusst nicht übernommen: das Ratio ist aus den
    beiden Lasten ableitbar, der Comment ist Fließtext ohne Spalte.
    """
    check_response(text)
    out: dict[str, dict] = {}
    day: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        m = _PLAIN_DAY.match(stripped)
        if m:
            day = m.group(1)
            continue
        if not day:
            continue
        for label, col in (("Short-Term Load", "training_load_short"),
                           ("Long-Term Load", "training_load_long")):
            if stripped.startswith(label + ":"):
                fields = out.setdefault(day, {})
                _put(fields, col, parse_int(stripped.split(":", 1)[1]))
    return {d: f for d, f in out.items() if f}


def _parse_flat(text: str, label: str, col: str) -> dict:
    """Ein einzelnes 'Label: Wert' aus einer tageslosen Antwort."""
    check_response(text)
    fields: dict = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(label + ":"):
            _put(fields, col, parse_int(stripped.split(":", 1)[1]))
            break            # nur der erste Treffer, sonst gewinnt eine spätere Zeile
    return fields


def parse_fitness_overview(text: str) -> dict:
    """queryFitnessAssessmentOverview → {'vo2max': 47}. Ohne Tagesbezug."""
    return _parse_flat(text, "VO2max", "vo2max")


def parse_recovery(text: str) -> dict:
    """queryRecoveryStatus → {'recovery_pct': 82}. Ohne Tagesbezug."""
    return _parse_flat(text, "Recovery", "recovery_pct")
