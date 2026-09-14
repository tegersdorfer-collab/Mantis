"""Der Morgenbericht — was die Nacht gebracht hat, in einem Text.

`forge.cli status` druckt ihn; Plan 3 schickt denselben Text per Telegram.
Deshalb reiner Text, keine Tabellen, keine Farben.
"""
from forge import budget, queue
from forge import models as m


def _zeile_freigabe(t: dict) -> str:
    return f"  #{t['id']} {t['title']}  →  python3.14 -m forge.cli approve {t['id']}"


def _zeile_geparkt(t: dict) -> str:
    return f"  #{t['id']} {t['title']} — {t.get('parked_reason') or 'ohne Grund'}"


def _zeile_anbieter(z: dict) -> str:
    if z.get("erschoepft_seit"):
        return (f"  {z['provider']}: leer seit {z['erschoepft_seit']} ({z.get('grund') or '?'}), "
                f"{z['laeufe']} Läufe")
    return f"  {z['provider']}: {z['laeufe']} Läufe"


def morgenbericht() -> str:
    freigabe = queue.nach_zustand(m.AWAITING_APPROVAL)
    geparkt = queue.nach_zustand(m.PARKED)
    stand = budget.stand()

    teile = ["Forge — Morgenbericht", ""]
    teile.append("Zur Freigabe: " + (f"{len(freigabe)}" if freigabe else "keine"))
    teile += [_zeile_freigabe(t) for t in freigabe]
    teile.append("")
    teile.append("Geparkt: " + (f"{len(geparkt)}" if geparkt else "keine"))
    teile += [_zeile_geparkt(t) for t in geparkt]
    teile.append("")
    teile.append("Anbieter:" if stand else "Anbieter: keine Läufe diese Nacht")
    teile += [_zeile_anbieter(z) for z in stand]
    return "\n".join(teile).rstrip() + "\n"
