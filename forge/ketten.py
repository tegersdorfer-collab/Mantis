"""Welches Modell eine Stufe benutzt, wenn das bevorzugte nicht mehr kann.

Eine Kette ist geordnet: das erste Glied ist die erste Wahl. Ist sein Anbieter
erschöpft, rückt das nächste nach. Ist die Kette leer, liefert `waehle` None —
der Aufrufer parkt den Task dann, statt ihn scheitern zu lassen.

Alle Modelle sind am 2026-09-07 gegen die echten Anbieter geprüft worden;
DeepSeek V4 (Zeitüberschreitung), kimi-k2.6 und nemotron-ultra-253b (404 für
diesen Account) und gemma-4-31b (Zeitüberschreitung) stehen deshalb NICHT
hier drin, obwohl Kataloge sie führen.
"""
from forge import budget

# Stufenname → geordnete Folge von (backend, model).
KETTEN: dict[str, tuple[tuple[str, str], ...]] = {
    # Gemessen am 2026-09-15 (Direkt-Ping + Agent-Loop durch runner_opencode,
    # Aufgabe: Datei lesen, Datei rückwärts schreiben, Pfad melden):
    #   nemotron-3-super-120b      8,5 s, korrekt          -> erstes Glied überall
    #   nemotron-3.5-lightning    30 s, Datei mit Fehlern  -> Reserve
    #   nemotron-3-nano-omni      72 s, Datei mit Fehlern  -> Reserve
    #   gpt-oss-20b              109 s, keine Datei        -> raus
    #   kimi-k3                   60 s ohne ein "OK"       -> raus
    #   minimax-m3                seit 2026-09-09 abgeschaltet (410) -> raus
    #   gemini-3.6-flash          gut, aber 20 Requests/TAG je Modell im Free
    #                             Tier — ein Agent-Loop frisst das in einer
    #                             Stufe -> nur letztes Glied, nie erstes
    # Erschöpfung gilt anbieterweit; das letzte Glied liegt deshalb bei einem
    # anderen Anbieter (google), damit ein NVIDIA-Rate-Limit die Stufe nicht
    # sofort trockenlegt.
    "spec": (
        ("opencode", "nvidia/nvidia/nemotron-3-super-120b-a12b"),
        ("opencode", "nvidia/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"),
        ("opencode", "google/gemini-3.6-flash"),
    ),
    "plan": (
        ("opencode", "nvidia/nvidia/nemotron-3-super-120b-a12b"),
        ("opencode", "nvidia/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"),
        ("opencode", "google/gemini-3.6-flash"),
    ),
    "implement": (
        ("opencode", "nvidia/nvidia/nemotron-3-super-120b-a12b"),
        ("opencode", "nvidia/nvidia/nemotron-3.5-lightning-30b-a3b"),
        ("opencode", "google/gemini-3.6-flash"),
    ),
    "fix": (
        ("opencode", "nvidia/nvidia/nemotron-3-super-120b-a12b"),
        ("opencode", "nvidia/nvidia/nemotron-3.5-lightning-30b-a3b"),
        ("opencode", "google/gemini-3.6-flash"),
    ),
    # Review ist bewusst Antigravity-only, und die Kette ist deshalb kürzer als
    # die anderen. Die Review-Stufe hat ein PROTOKOLL: der Diff wird aus
    # .forge/diff.patch gelesen und in den Prompt eingebettet, das Urteil kommt
    # auf stdout, und der Runner schreibt daraus .forge/review.json. Nur
    # forge/runner_agy.py kann das; forge/runner_opencode.py kennt weder die
    # eine noch die andere Datei. Zwei Glieder sind die ehrliche Kettenlänge,
    # solange nur ein Backend das Protokoll kann.
    "review": (
        ("agy", "claude-opus-4-6-thinking"),
        ("agy", "gemini-3.1-pro-high"),
    ),
}


def waehle(stufe_name: str, verboten: frozenset[str] = frozenset()) -> tuple[str, str] | None:
    """Das erste nutzbare Glied der Kette, oder None.

    `verboten` trägt die Reviewer-Regel: die Review-Stufe bekommt hier das
    Modell hinein, mit dem implementiert wurde. Bleibt danach nichts übrig,
    ist None die richtige Antwort — der Task wird geparkt statt von dem
    Modell abgenommen, das ihn geschrieben hat.
    """
    for backend, model in KETTEN.get(stufe_name, ()):
        if model in verboten:
            continue
        if budget.ist_erschoepft(model):
            continue
        return backend, model
    return None


def kette_erschoepft(stufe_name: str) -> bool:
    """Ist die Kette wirklich trocken — unabhängig von der Reviewer-Regel?

    `waehle` liefert None aus zwei Gründen, die NICHT denselben Handler haben
    dürfen:

    * **Reviewer-Kollision** — es bliebe nur noch das Modell übrig, das
      implementiert hat. Die Spec verlangt hier Parken (Zeile 183: "geparkt
      statt reviewt").
    * **Kontingent-Erschöpfung** — jeder Anbieter der Kette ist für diese
      Nacht raus. Die Spec sagt hier etwas anderes (Zeile 264: "Erst wenn jede
      Kette trocken ist, endet die Nacht") — der Task bleibt liegen und läuft
      weiter, sobald das Kontingent zurück ist.

    Unterschieden wird durch einen zweiten Anlauf OHNE `verboten`: bleibt auch
    dann nichts übrig, lag es nicht an der Reviewer-Regel. Diesen Weg statt
    eines zusätzlichen Rückgabewerts von `waehle`, weil `waehle` genau einen
    Zweck hat (welches Glied läuft?) und ein Tupel aus Wahl und Grund jeden
    Aufrufer zwänge, den Grund auch dort auszupacken, wo er ihn nicht braucht.
    Der Preis ist ein zweiter Durchlauf der Kette — er passiert nur im
    None-Fall, also nie im Normalbetrieb.
    """
    return waehle(stufe_name) is None
