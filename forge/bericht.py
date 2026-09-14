"""Der Morgenbericht — was die Nacht gebracht hat, in einem Text.

`forge.cli status` druckt ihn; Plan 3 schickt denselben Text per Telegram.
Deshalb reiner Text, keine Tabellen, keine Farben.
"""
from datetime import datetime

from forge import budget, daemon, journal, queue
from forge import models as m


def _zeile_freigabe(t: dict) -> str:
    return f"  #{t['id']} {t['title']}  →  python3.14 -m forge.cli approve {t['id']}"


def _zeile_geparkt(t: dict) -> str:
    return f"  #{t['id']} {t['title']} — {t.get('parked_reason') or 'ohne Grund'}"


def _uhrzeit(wert) -> str:
    """Ein TIMESTAMPTZ aus Postgres kommt als datetime — im Bericht reicht die
    Uhrzeit, die Nacht ist bekannt. Strings (Fixtures, Altbestand) bleiben, wie
    sie sind."""
    if isinstance(wert, datetime):
        return wert.strftime("%H:%M")
    return str(wert)


def _zeitpunkt(wert) -> str:
    """Für die Daemon-Zeile mit Datum: Start und Ende liegen auf zwei Tagen."""
    if isinstance(wert, datetime):
        return wert.strftime("%Y-%m-%d %H:%M")
    return str(wert)


def _zeile_anbieter(z: dict) -> str:
    # Abschluss-Review 2c, I4 (c): der Nachtrag verlangt "Verbrauch je
    # Anbieter" — Läufe allein sagen nichts über das Kontingent.
    verbrauch = f"{z['laeufe']} Läufe, {z.get('tokens_in') or 0} rein / {z.get('tokens_out') or 0} raus"
    if z.get("erschoepft_seit"):
        return (f"  {z['provider']}: leer seit {_uhrzeit(z['erschoepft_seit'])} "
                f"({z.get('grund') or '?'}), {verbrauch}")
    return f"  {z['provider']}: {verbrauch}"


def _zeile_not_aus() -> str:
    """Abschluss-Review 2c, I4 (a): steht der Not-Aus, sieht "Zur Freigabe:
    keine" genauso aus wie nach einer Nacht ohne Arbeit. Der Inhalt der Datei
    ist der Grund (Fehler-Spirale) — oder leer, wenn Timo sie per touch
    angelegt hat."""
    if not daemon.STOP_FILE.exists():
        return "Not-Aus: nein"
    try:
        inhalt = daemon.STOP_FILE.read_text().strip()
    except OSError:
        inhalt = ""
    return f"Not-Aus: aktiv — {inhalt}" if inhalt else "Not-Aus: aktiv"


def _zeile_daemon(ereignisse: dict) -> str:
    """Abschluss-Review 2c, I4 (b): hat die Nacht stattgefunden, und wie
    endete sie? Ein daemon_stop, der ÄLTER als der letzte Start ist, gehört
    zur vorigen Nacht — dann fehlt das Ende, und das ist die Information
    (Absturz oder noch am Laufen)."""
    start = ereignisse.get("daemon_start")
    if not start:
        return "Daemon: kein Start im Journal"
    stop = ereignisse.get("daemon_stop")
    if stop and _liegt_nach(stop.get("ts"), start.get("ts")):
        return (f"Daemon: gestartet {_zeitpunkt(start.get('ts'))}, "
                f"beendet {_zeitpunkt(stop.get('ts'))} ({stop.get('message') or 'ohne Nachricht'})")
    return f"Daemon: gestartet {_zeitpunkt(start.get('ts'))}, kein Ende im Journal"


def _liegt_nach(spaeter, frueher) -> bool:
    try:
        return spaeter >= frueher
    except TypeError:
        # Unvergleichbar (z.B. None) — lieber "kein Ende" melden als raten.
        return False


def morgenbericht() -> str:
    freigabe = queue.nach_zustand(m.AWAITING_APPROVAL)
    geparkt = queue.nach_zustand(m.PARKED)
    stand = budget.stand()
    ereignisse = journal.letzte_daemon_ereignisse()

    teile = ["Forge — Morgenbericht", ""]
    teile.append(_zeile_daemon(ereignisse))
    teile.append(_zeile_not_aus())
    teile.append("")
    teile.append("Zur Freigabe: " + (f"{len(freigabe)}" if freigabe else "keine"))
    teile += [_zeile_freigabe(t) for t in freigabe]
    teile.append("")
    teile.append("Geparkt: " + (f"{len(geparkt)}" if geparkt else "keine"))
    teile += [_zeile_geparkt(t) for t in geparkt]
    teile.append("")
    teile.append("Anbieter:" if stand else "Anbieter: keine Läufe diese Nacht")
    teile += [_zeile_anbieter(z) for z in stand]
    return "\n".join(teile).rstrip() + "\n"
