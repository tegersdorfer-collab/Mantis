"""COROS-Prosa → health_data-Spalten. Reine Funktionen, kein I/O.

Das COROS-MCP liefert keine strukturierten Daten: kein outputSchema, kein
structuredContent, nur LLM-erzeugten Text mit fester Zeilenstruktur. Dieses Modul
holt daraus die Zahlen.

Leitlinie ist Strenge. Ein fehlender Wert ist harmlos — health_scores meldet dann
ehrlich "zu wenig Daten". Ein falsch geparster Wert sieht aus wie eine Messung und
verschiebt die Baselines dauerhaft. Deshalb: exakte Muster, Plausibilitätsgrenzen,
und im Zweifel nichts.

Fehlererkennung läuft zweistufig. Zuerst `check_response`, eine Denyliste
bekannter Fehlertexte (`_ERROR_MARKERS`) — die erklärt einen Fehler mit einer
präzisen Meldung, kann aber naturgemäß nur Formulierungen kennen, die schon
einmal aufgetreten sind. Als Netz darunter prüft jeder Parser strukturell, ob
er überhaupt erkennbare Struktur gesehen hat (mindestens ein Tagesmarker bei
den tagesbezogenen Parsern, das erwartete Label bei den tageslosen). Fehlt
beides, ist ein leeres Ergebnis keine ehrliche "keine Daten"-Aussage mehr,
sondern ein unerkanntes Format — dann `CorosFormatError` statt stillem `{}`.
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
    """Text durchreichen — oder CorosFormatError bei einer der bekannten Fehlerformulierungen.

    Das ist die erste, präzise Stufe der Fehlererkennung (siehe Moduldocstring).
    Was hier nicht matcht, aber trotzdem kein verwertbares Format ist, fängt die
    zweite Stufe ab: die Strukturprüfung in den einzelnen Parsern weiter unten.
    """
    if not text or not text.strip():
        raise CorosFormatError("Leere Antwort vom COROS-MCP")
    low = text.lower()
    for marker in _ERROR_MARKERS:
        if marker in low:
            raise CorosFormatError(f"COROS meldet einen Fehler statt Daten: {text[:160]}")
    return text


def parse_int(raw: str) -> int | None:
    """'12,500' → 12500. '12500' → 12500. 'No data', Leerstring und Unsinn → None.

    Zwei Formen sind erlaubt: eine Zahl ganz ohne Komma (beliebig viele Stellen),
    oder eine mit Komma, dann aber nur mit wohlgeformten Dreiergruppen. Ein
    abgeschnittener Stream (SSE-Antwort bricht mitten in der Zahl ab) darf nicht
    als Zahl mit weniger Stellen durchgehen — '1,0' ist keine 10 und keine 1,
    sondern gar nichts. Die Lookbehind/Lookahead-Guards verhindern, dass die
    Regex bei einem fehlgeschlagenen Dreiergruppen-Match einfach auf das
    führende Fragment zurückfällt (z.B. '1' aus '1,0') oder ein Fragment nach
    dem Komma als eigene Zahl aufgreift (z.B. '0' aus '1,0').
    """
    if raw is None:
        return None
    m = re.search(r"(?<![\d,])-?(?:\d{1,3}(?:,\d{3})+|\d+)(?![\d,])", raw)
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


def parse_float(raw: str) -> float | None:
    """'61.4 kg' → 61.4, '42 ms' → 42.0, 'No data' → None."""
    if raw is None:
        return None
    m = re.search(r"-?\d+(?:[.,]\d+)?", raw)
    return float(m.group().replace(",", ".")) if m else None


def parse_clock_sec(raw: str) -> int | None:
    """'5:00' → 300, '1:40:24' → 6024. Uhrzeit-Notation in Sekunden.

    Zwei Gruppen sind Minuten:Sekunden, drei sind Stunden:Minuten:Sekunden — so
    liefert COROS Schwellentempo und Renn-Prognosen.
    """
    if raw is None:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", raw)
    if not m:
        return None
    a, b, c = m.group(1), m.group(2), m.group(3)
    if c is None:
        return int(a) * 60 + int(b)
    return int(a) * 3600 + int(b) * 60 + int(c)


def _iso_date(raw: str) -> str | None:
    """'1994-11-02 (Age: 31)' → '1994-11-02'."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", raw or "")
    return m.group() if m else None


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
    "sleep_awake_count": (0, 50),
    "nap_duration":     (0, 12),
    "sleep_hr_avg":     (20, 220),
    "sleep_hr_min":     (20, 220),
    "sleep_hr_max":     (20, 250),
    "training_load_ratio": (0, 10),
    "hrv_baseline":     (1, 300),
    "running_level":    (0, 200),
    "threshold_pace_sec": (60, 1_200),
    "race_5k_sec":      (300, 36_000),
    "race_10k_sec":     (600, 36_000),
    "race_half_sec":    (1_200, 72_000),
    "race_marathon_sec": (2_400, 144_000),
    "recovery_full_h":  (0, 336),
    "height_cm":        (50, 250),
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


def _put_text(target: dict, col: str, value: str | None) -> None:
    """Für Spalten, die Text tragen (Uhrzeiten, COROS' eigene Einschätzungen).

    Getrennt von _put, weil LIMITS numerisch vergleicht — ein String dort hinein
    wäre ein TypeError. Geprüft wird nur, dass überhaupt etwas dasteht.
    """
    if value and (v := value.strip()):
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

    Fehlt jeder Tagesmarker ('--- JJJJMMTT ---'), wird das nicht als leeres
    Ergebnis gewertet, sondern als CorosFormatError — siehe Moduldocstring.
    """
    check_response(text)
    out: dict[str, dict] = {}
    day: str | None = None
    saw_day_marker = False

    for line in text.splitlines():
        stripped = line.strip()

        m = _DAY_RE.match(stripped)
        if m:
            saw_day_marker = True
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
        elif stripped.startswith("Sleep HR:"):
            for label, col in (("Avg", "sleep_hr_avg"), ("Min", "sleep_hr_min"),
                               ("Max", "sleep_hr_max")):
                mm = re.search(rf"\b{label}\s+(\d+)", stripped)
                if mm:
                    _put(fields, col, parse_int(mm.group(1)))
        elif stripped.startswith("Total:"):
            for label, col in (("Total", "sleep_duration"), ("Deep", "sleep_deep"),
                               ("Light", "sleep_core"), ("REM", "sleep_rem"),
                               ("Awake", "sleep_awake")):
                mm = re.search(rf"\b{label}:\s*([^|]+)", stripped)
                if mm:
                    _put(fields, col, parse_duration_h(mm.group(1)))

    if not saw_day_marker:
        raise CorosFormatError(
            "parse_daily_health: keine Tagesmarker ('--- JJJJMMTT ---') gefunden "
            "— Format vermutlich geändert"
        )

    # Tage ohne einen einzigen erkannten Wert wieder rauswerfen — sonst schreibt der
    # Importer leere Zeilen und die Scores halten den Tag für erfasst.
    return {d: f for d, f in out.items() if f}


_ISO_DAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}):?$")


# COROS nennt die Leichtschlafphase "Light", health_data nennt die Spalte
# sleep_core — dieselbe Phase, anderer Name.
_WINDOW_RE = re.compile(r"(\d{1,2}:\d{2})\s*-\s*\S+\s+(\d{1,2}:\d{2})")

_STAGE_RATIOS = {
    "Deep Sleep Ratio": "sleep_deep",
    "Light Sleep Ratio": "sleep_core",
    "REM Ratio": "sleep_rem",
}


def parse_sleep(text: str) -> dict[str, dict]:
    """querySleepData → {'2026-08-10': {'sleep_score': 77, 'sleep_deep': 1.56, …}, …}.

    Die Phasen stehen hier als Prozentanteil der Hauptschlafzeit; die absoluten
    Stunden werden daraus gerechnet. parse_daily_health liefert dieselben Phasen
    als exakte Dauer, aber nur für die jüngsten Tage — deshalb steht diese Quelle
    in _DAY_SOURCES *vor* ihr und wird von der genaueren überschrieben.

    Fehlt jeder Tagesmarker (JJJJ-MM-TT), wird das nicht als leeres Ergebnis
    gewertet, sondern als CorosFormatError — siehe Moduldocstring.
    """
    check_response(text)
    out: dict[str, dict] = {}
    saw_day_marker = False
    day: str | None = None
    raw: dict = {}

    def flush() -> None:
        if day is None:
            return
        fields: dict = {}
        _put(fields, "sleep_score", raw.get("score"))
        _put(fields, "sleep_awake_count", raw.get("awake_count"))
        _put(fields, "nap_duration", raw.get("nap_h"))
        _put_text(fields, "sleep_start", raw.get("start"))
        _put_text(fields, "sleep_end", raw.get("end"))
        main_h = raw.get("main_h")
        # 0 h Hauptschlaf heißt "Uhr nicht getragen", nicht "null Stunden
        # geschlafen" — daraus abgeleitete Phasen wären erfundene Nullen.
        if main_h:
            _put(fields, "sleep_duration", main_h)
            _put(fields, "sleep_awake", raw.get("awake_h"))
            for label, col in _STAGE_RATIOS.items():
                pct = raw.get(label)
                if pct is not None:
                    _put(fields, col, round(main_h * pct) / 100)
        if fields:
            out[day] = fields

    for line in text.splitlines():
        stripped = line.strip()
        m = _ISO_DAY_RE.match(stripped)
        if m:
            flush()
            saw_day_marker = True
            day = m.group(1)
            raw = {}
            continue
        if not day or ":" not in stripped:
            continue
        label, value = (p.strip() for p in stripped.split(":", 1))
        if label == "Sleep Score":
            raw["score"] = parse_int(value)
        elif label == "Main Sleep":
            raw["main_h"] = parse_duration_h(value)
        elif label == "Awake Time":
            raw["awake_h"] = parse_duration_h(value)
        elif label == "Naps Total":
            raw["nap_h"] = parse_duration_h(value)
        elif label.startswith("Awake Count"):
            raw["awake_count"] = parse_int(value)
        elif label == "Main Sleep Window":
            # "2026-08-09 22:35 - 2026-08-10 05:58" → Einschlaf- und Aufwachzeit.
            # Nur die Uhrzeiten; das Datum steckt schon im Tagesmarker.
            if w := _WINDOW_RE.search(stripped):
                raw["start"], raw["end"] = w.group(1), w.group(2)
        elif label in _STAGE_RATIOS:
            raw[label] = parse_int(value)
    flush()

    if not saw_day_marker:
        raise CorosFormatError(
            "parse_sleep: keine Tagesmarker (JJJJ-MM-TT) gefunden — Format vermutlich geändert"
        )
    return out


def parse_sleep_hrv(text: str) -> dict[str, dict]:
    """querySleepHrv → {'2026-08-12': {'hrv': 71}, …}.

    Liest nur den Tagesabschnitt. Danach folgt eine Rohzeitreihe mit zehntausenden
    'timestamp=… hrv=…'-Zeilen; die wird übersprungen, weil sie keine Tagesmarker hat.

    Fehlt jeder Tagesmarker (JJJJ-MM-TT), wird das nicht als leeres Ergebnis
    gewertet, sondern als CorosFormatError — siehe Moduldocstring.
    """
    check_response(text)
    out: dict[str, dict] = {}
    day: str | None = None
    saw_day_marker = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("timestamp="):
            day = None            # Rohzeitreihe erreicht — ab hier nichts mehr übernehmen
            continue
        m = _ISO_DAY_RE.match(stripped)
        if m:
            saw_day_marker = True
            day = m.group(1)
            continue
        if day and stripped.startswith("HRV Avg:"):
            fields: dict = {}
            _put(fields, "hrv", parse_int(stripped.split(":", 1)[1]))
            if fields:
                out[day] = fields
            day = None            # 'Baseline:' darunter darf den Wert nicht ersetzen
    if not saw_day_marker:
        raise CorosFormatError(
            "parse_sleep_hrv: keine Tagesmarker (JJJJ-MM-TT) gefunden — Format vermutlich geändert"
        )
    return out


_ANY_DAY_LINE = re.compile(r"^\d{4}-\d{2}-\d{2}:")


def _parse_day_value_lines(text: str, pattern: re.Pattern, build, name: str) -> dict[str, dict]:
    """Gemeinsame Form für 'YYYY-MM-DD: …'-Zeilen: je Treffer ein Tag.

    Die Strukturprüfung läuft bewusst über `_ANY_DAY_LINE`, ein permissives
    'beginnt mit Datum und Doppelpunkt' — NICHT über `pattern`. `pattern`
    verlangt zusätzlich einen Wert (z.B. 'NN bpm', anchored per Vorgängerfix)
    und matcht darum eine Zeile wie '2026-08-10: No data' gar nicht. Würde die
    Strukturprüfung `pattern` selbst benutzen, sähe eine Woche voller
    'No data'-Tage wie gar keine Struktur aus und würde fälschlich einen
    CorosFormatError auslösen, obwohl Fall 1 (Tag erkannt, keine Messung)
    vorliegt.
    """
    check_response(text)
    out: dict[str, dict] = {}
    saw_day_marker = False
    for line in text.splitlines():
        stripped = line.strip()
        if _ANY_DAY_LINE.match(stripped):
            saw_day_marker = True
        m = pattern.match(stripped)
        if not m:
            continue
        fields = build(m)
        if fields:
            out[m.group(1)] = fields
    if not saw_day_marker:
        raise CorosFormatError(
            f"{name}: keine Tagesmarker (JJJJ-MM-TT:) gefunden — Format vermutlich geändert"
        )
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
    return _parse_day_value_lines(text, _RHR_LINE, build, "parse_resting_hr")


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
    return _parse_day_value_lines(text, _AVG_HR_LINE, build, "parse_avg_hr")


_PLAIN_DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})$")


def parse_training_load(text: str) -> dict[str, dict]:
    """queryTrainingLoadAssessment → kurz-/langfristige Last je Tag.

    Load Ratio und Comment werden bewusst nicht übernommen: das Ratio ist aus den
    beiden Lasten ableitbar, der Comment ist Fließtext ohne Spalte.

    Fehlt jeder Tagesmarker (JJJJ-MM-TT), wird das nicht als leeres Ergebnis
    gewertet, sondern als CorosFormatError — siehe Moduldocstring.
    """
    check_response(text)
    out: dict[str, dict] = {}
    day: str | None = None
    saw_day_marker = False
    for line in text.splitlines():
        stripped = line.strip()
        m = _PLAIN_DAY.match(stripped)
        if m:
            saw_day_marker = True
            day = m.group(1)
            continue
        if not day:
            continue
        for label, col, conv in (("Short-Term Load", "training_load_short", parse_int),
                                 ("Long-Term Load", "training_load_long", parse_int),
                                 ("Load Ratio", "training_load_ratio", parse_float),
                                 ("Comment", "training_load_trend", _text)):
            if stripped.startswith(label + ":"):
                fields = out.setdefault(day, {})
                value = conv(stripped.split(":", 1)[1].strip())
                if isinstance(value, str):
                    _put_text(fields, col, value)
                else:
                    _put(fields, col, value)
    if not saw_day_marker:
        raise CorosFormatError(
            "parse_training_load: keine Tagesmarker (JJJJ-MM-TT) gefunden — Format vermutlich geändert"
        )
    return {d: f for d, f in out.items() if f}


def _parse_flat(text: str, specs, anchor: str, name: str) -> dict:
    """Mehrere 'Label: Wert' aus einer tageslosen Antwort.

    specs ist eine Folge von (Label, Spalte, Konverter). Liefert der Konverter
    einen String, geht der Wert über _put_text (keine numerische Grenze), sonst
    über _put.

    Die Strukturprüfung fragt "wurde das Anker-Label gesehen", nicht "wurde ein
    Wert extrahiert" — 'Recovery: No data' hat das Label, nur der Wert fehlt
    (Fall 1, legitim). Fehlt es komplett, hat sich das Format vermutlich geändert.
    """
    check_response(text)
    fields: dict = {}
    seen_anchor = False
    for line in text.splitlines():
        stripped = line.strip()
        for label, col, conv in specs:
            # Nur der erste Treffer je Spalte, sonst gewinnt eine spätere Zeile.
            if col in fields or not stripped.startswith(label + ":"):
                continue
            if label == anchor:
                seen_anchor = True
            # .strip(): parse_duration_h würde bei ' 4h' am führenden Leerzeichen
            # einen Leer-Match liefern und None zurückgeben.
            value = conv(stripped.split(":", 1)[1].strip())
            if isinstance(value, str):
                _put_text(fields, col, value)
            else:
                _put(fields, col, value)
    if not seen_anchor:
        raise CorosFormatError(
            f"{name}: Label '{anchor}' nicht gefunden — Format vermutlich geändert"
        )
    return fields


def _text(raw: str) -> str:
    return raw.strip()


_FITNESS_SPECS = (
    ("VO2max", "vo2max", parse_int),
    ("Running Level", "running_level", parse_int),
    ("Threshold Pace", "threshold_pace_sec", parse_clock_sec),
    ("5 km Prediction", "race_5k_sec", parse_clock_sec),
    ("10 km Prediction", "race_10k_sec", parse_clock_sec),
    ("Half Marathon Prediction", "race_half_sec", parse_clock_sec),
    ("Marathon Prediction", "race_marathon_sec", parse_clock_sec),
)

_RECOVERY_SPECS = (
    ("Recovery", "recovery_pct", parse_int),
    ("Level", "recovery_level", _text),
    ("Estimated Full Recovery", "recovery_full_h", parse_duration_h),
)

_USER_SPECS = (
    ("Weight", "weight", parse_float),
    ("Height", "height_cm", parse_int),
    ("Birthday", "birthday", _iso_date),
    ("Gender", "gender", _text),
    ("Nickname", "nickname", _text),
)


def parse_fitness_overview(text: str) -> dict:
    """queryFitnessAssessmentOverview → VO2max, Laufniveau, Schwellentempo und
    die vier Renn-Prognosen. Ohne Tagesbezug — ein Momentanwert.

    Tempo und Prognosen stehen als Uhrzeit ('4:31 /km', '1:40:24') und werden in
    Sekunden abgelegt: vergleichbar, rechenbar, kein Parsen beim Auslesen.
    """
    return _parse_flat(text, _FITNESS_SPECS, "VO2max", "parse_fitness_overview")


def parse_recovery(text: str) -> dict:
    """queryRecoveryStatus → Erholung in %, COROS' Einschätzung als Text und die
    geschätzte Restzeit bis zur vollen Erholung. Ohne Tagesbezug."""
    return _parse_flat(text, _RECOVERY_SPECS, "Recovery", "parse_recovery")


def parse_user_info(text: str) -> dict:
    """queryUserInfo → Gewicht plus die konstanten Profildaten.

    Der Importer trennt sie: `weight` ist ein Tageswert und geht nach health_data,
    der Rest ändert sich nicht täglich und landet in der settings-Tabelle.
    """
    return _parse_flat(text, _USER_SPECS, "Weight", "parse_user_info")


def parse_daily_header(text: str) -> dict:
    """Kopfzeile von queryDailyHealthData → {'hrv_baseline': 50}.

    Die Zeile 'Daily Health Data — Last N days | Resting HR: … | HRV Baseline: …'
    gilt für den gesamten Abruf, nicht für einen Tag — deshalb ein eigener Parser
    mit Ziel health_assessment. 'Resting HR' wird hier nicht übernommen:
    queryRestingHeartRate liefert denselben Wert mit Tagesbezug.
    """
    check_response(text)
    fields: dict = {}
    head = text.splitlines()[0] if text.strip() else ""
    if m := re.search(r"HRV Baseline:\s*([\d.,]+)", head):
        _put(fields, "hrv_baseline", parse_float(m.group(1)))
    return fields
