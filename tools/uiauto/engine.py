"""UI-Automatik-Engine — dünner Wrapper um die OSS-Bibliothek atomacos.

Design: Die öffentliche Grenze (is_trusted/snapshot/act/type_text/press_key) ist
rein und unit-testbar; die atomacos-spezifischen Aufrufe stecken in kleinen
Helfern (_ax_is_trusted/_get_app/_raw_elements/_wake/_do_type/_do_key/_perform),
die Tests mocken (Muster wie tools/flipper). atomacos wird LAZY importiert, damit
ein fehlendes Paket die Skill-Registrierung / den Mantis-Start nicht crasht.

Läuft nur mit „Bedienungshilfen"-Recht (System Settings → Datenschutz →
Bedienungshilfen) für den Python-Interpreter. Ohne das wirft snapshot() einen
klaren UIAutoError; der aufrufende Skill übersetzt das in eine Setup-Meldung.
"""
from __future__ import annotations

import logging

log = logging.getLogger("core.skills")


class UIAutoError(RuntimeError):
    """UI-Automatik nicht möglich (kein Recht, App weg, atomacos fehlt, …)."""


# Aktionable Rollen — nur diese landen im Snapshot (der Rest ist Deko/Layout).
ACTIONABLE_ROLES: frozenset[str] = frozenset({
    "AXButton", "AXMenuItem", "AXMenuButton", "AXCheckBox", "AXRadioButton",
    "AXTextField", "AXTextArea", "AXSecureTextField", "AXPopUpButton",
    "AXLink", "AXRow", "AXCell", "AXTab",
})

# Zuletzt erzeugter Snapshot — act(ref) nutzt die rohen atomacos-Elemente,
# element(ref) die Dict-Sicht (für den Safety-Check im Skill).
_last_elements: list = []
_last_dicts: list[dict] = []


# ── atomacos-Grenze (nur live; Tests mocken diese Helfer) ─────────────────────

def _atomacos():
    try:
        import atomacos
        return atomacos
    except Exception as e:  # pragma: no cover - live-only
        raise UIAutoError(f"atomacos nicht verfügbar: {e}")


def _ax_is_trusted() -> bool:  # pragma: no cover - live-only
    try:
        from ApplicationServices import AXIsProcessTrusted
        return bool(AXIsProcessTrusted())
    except Exception:
        return False


def _get_app(app: str | None = None):  # pragma: no cover - live-only
    ax = _atomacos()
    if app:
        ref = ax.getAppRefByLocalizedName(app)
        if ref is None:
            raise UIAutoError(f"App ‚{app}' nicht gefunden oder nicht offen.")
        return ref
    return ax.getFrontmostApp()


def _wake(appobj) -> None:  # pragma: no cover - live-only
    """Weckt Chromium-Accessibility bei Electron-Apps (AXManualAccessibility).

    Native Apps unterstützen das Attribut nicht → Fehler ignorieren.
    """
    try:
        from ApplicationServices import AXUIElementSetAttributeValue
        AXUIElementSetAttributeValue(appobj.ref, "AXManualAccessibility", True)
    except Exception:
        pass


def _raw_elements(appobj) -> list:  # pragma: no cover - live-only
    return appobj.findAllR() or []


def _perform(raw, action: str) -> None:  # pragma: no cover - live-only
    name = action[2:] if action.startswith("AX") else action
    getattr(raw, name)()


def _do_type(text: str) -> None:  # pragma: no cover - live-only
    _atomacos().getFrontmostApp().sendKeys(text)


def _do_key(chord: str) -> None:  # pragma: no cover - live-only
    parts = [p.strip().lower() for p in chord.split("+") if p.strip()]
    if not parts:
        return
    key, mods = parts[-1], parts[:-1]
    app = _atomacos().getFrontmostApp()
    if mods:
        app.sendGlobalKeyWithModifiers(key, mods)
    else:
        app.sendGlobalKey(key)


def _attr(raw, name: str, default):
    try:
        v = getattr(raw, name)
    except Exception:
        return default
    return default if v is None else v


# ── Öffentliche, unit-testbare API ────────────────────────────────────────────

def is_trusted() -> bool:
    """Hat der Prozess das Bedienungshilfen-Recht?"""
    return _ax_is_trusted()


def snapshot(app: str | None = None) -> list[dict]:
    """Liste der bedienbaren Elemente der Vordergrund- (oder benannten) App.

    Element = {ref, role, title, value, enabled, visible}. ref = Index; darüber greift
    act(ref) das Element später wieder. Nur ACTIONABLE_ROLES.
    """
    if not is_trusted():
        raise UIAutoError(
            "Kein Bedienungshilfen-Recht. Bitte den Python-Interpreter unter "
            "Systemeinstellungen → Datenschutz & Sicherheit → Bedienungshilfen "
            "freigeben.")
    appobj = _get_app(app)
    _wake(appobj)
    raws = _raw_elements(appobj)
    out: list[dict] = []
    kept: list = []
    for raw in raws:
        role = _attr(raw, "AXRole", "")
        if role not in ACTIONABLE_ROLES:
            continue
        out.append({
            "ref": len(out),
            "role": role,
            "title": str(_attr(raw, "AXTitle", "") or ""),
            "value": str(_attr(raw, "AXValue", "") or ""),
            "enabled": bool(_attr(raw, "AXEnabled", True)),
            "visible": bool(_attr(raw, "AXVisible", True)),
        })
        kept.append(raw)
    global _last_elements, _last_dicts
    _last_elements = kept
    _last_dicts = out
    return out


def element(ref: int) -> dict | None:
    """Dict-Sicht des Elements aus dem letzten Snapshot (oder None bei ungültigem
    ref) — für den Safety-Check, bevor act() aufgerufen wird."""
    if 0 <= ref < len(_last_dicts):
        return _last_dicts[ref]
    return None


def act(ref: int, action: str = "AXPress") -> None:
    """Führt eine Aktion (Standard: drücken/klicken) auf dem Element aus dem
    letzten Snapshot aus."""
    if ref < 0 or ref >= len(_last_elements):
        raise UIAutoError(f"Ungültige Element-Referenz {ref} (Snapshot hat "
                          f"{len(_last_elements)} Elemente).")
    _perform(_last_elements[ref], action)


def type_text(text: str) -> None:
    _do_type(text)


def press_key(chord: str) -> None:
    _do_key(chord)
