"""Backends für Forge-Läufe: welche CLI eine Stufe ausführt und mit welchen Rechten.

`forge/runner.py` (Claude Code) bleibt unverändert bestehen. Dieses Modul
ergänzt Backends für `opencode` und `agy` und die Schranke, die deren Rechte
festnagelt.
"""
from dataclasses import dataclass
from typing import Callable

# Werte, die opencode für eine Rechte-Kategorie akzeptiert.
_AKTIONEN = frozenset({"allow", "ask", "deny"})

# Kategorien, die NIEMALS erlaubt werden dürfen, mit der Aufnahme als Begründung.
# Quelle je Eintrag: tests/fixtures/permission_probe_opencode.md.
_VERBOTEN = {
    "bash": (
        "Probe (d), 2026-09-09: external_directory ist Pfad-Erkennung auf "
        "Shell-Argument-Ebene, kein Sandbox. Ein Pfad, der erst im Zielprozess "
        "entsteht (python3 -c \"open(chr(46)+...)\"), bricht aus und hat den "
        "Köder preisgegeben. bash=allow bedeutet damit keine Eingrenzung."
    ),
    "task": (
        "Probe (e), 2026-09-09: ein Subagent führt Befehle mit eigenen Rechten "
        "aus und umgeht damit die Bash-Beschränkung des Elternagenten. "
        "'whoami' lief trotz Whitelist, delegiert an den General Agent."
    ),
    "external_directory": (
        "Probe (d), 2026-09-09: siehe bash. Diese Kategorie ist die einzige "
        "Schranke gegen Lesezugriff ausserhalb des Worktrees und wird nicht "
        "gelockert."
    ),
}


@dataclass(frozen=True)
class OpencodePermission:
    """Die für unbeaufsichtigten Betrieb belegte Rechtekonfiguration.

    Die Defaults sind Probe (j) aus tests/fixtures/permission_probe_opencode.md.
    Die drei Kategorien in `_VERBOTEN` lassen sich nicht auf 'allow' setzen —
    nicht weil es unklug wäre, sondern weil eine Aufnahme belegt, dass die
    Schranke dann nicht hält. Eine Lockerung gehört erst dann hierher, wenn eine
    neue Aufnahme das Gegenteil zeigt.
    """
    read: str = "allow"
    edit: str = "allow"
    glob: str = "allow"
    grep: str = "allow"
    list: str = "allow"
    bash: str = "deny"
    task: str = "deny"
    webfetch: str = "deny"
    websearch: str = "deny"
    external_directory: str = "deny"

    def __post_init__(self):
        for feld, wert in self.als_dict().items():
            if wert not in _AKTIONEN:
                raise ValueError(
                    f"Ungültige Rechte-Aktion für {feld!r}: {wert!r} — "
                    f"erlaubt sind allow, ask, deny"
                )
            if wert != "deny" and feld in _VERBOTEN:
                raise ValueError(
                    f"Rechte-Kategorie {feld!r} darf nicht {wert!r} sein: "
                    f"{_VERBOTEN[feld]}"
                )

    def als_dict(self) -> dict[str, str]:
        """Die Konfiguration, wie opencode sie unter `agent.<name>.permission` erwartet."""
        return {
            "read": self.read, "edit": self.edit, "glob": self.glob,
            "grep": self.grep, "list": self.list,
            "bash": self.bash, "task": self.task,
            "webfetch": self.webfetch, "websearch": self.websearch,
            "external_directory": self.external_directory,
        }


def hole(name: str) -> Callable:
    """Die run-Funktion eines Backends.

    Der Import steht bewusst in der Funktion, nicht am Modulkopf: `runner_agy`
    importiert `forge.stages` (für DIFF_DATEI und VERDIKT_DATEI). Ein
    Modulimport hier hiesse, dass jeder Import von `forge.backends` die
    gesamte Stufen- und Prompt-Kette mitzieht — auch dort, wo nur die
    Rechteschranke gebraucht wird, etwa in tests/test_forge_backends.py.
    Heute gibt es keinen Zyklus; sobald `stages` je `backends` braucht,
    entstünde einer, und dieser Import verhindert ihn schon jetzt.
    """
    from forge import runner_agy, runner_opencode
    tabelle = {
        "opencode": runner_opencode.run,
        "agy": runner_agy.run,
    }
    if name not in tabelle:
        raise KeyError(f"unbekanntes Backend: {name!r}")
    return tabelle[name]
