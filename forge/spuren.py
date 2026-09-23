"""Optionale Forge-Traces über die externe ``tracelog``-CLI.

Die CLI verwaltet Records und Blobs. Dieses Modul hält nur die letzte Record-ID
je Problem sowie Backfill-Marker im benutzerweiten State, bereitet redigierte
Blob-Temporärdateien vor und schluckt jeden Trace-Fehler.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import uuid
from datetime import date, datetime
from functools import lru_cache
from functools import wraps
from pathlib import Path
from typing import Any

from core import db

log = logging.getLogger(__name__)
_STATE_LOCK = threading.Lock()
_TRACE_TIMEOUT = 10
_STATE_CACHE_PATH: str | None = None
_LAST_RECORD_IDS: dict[str, str] = {}

_REDACTIONS = (
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "anthropic-key"),
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "api-key"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "github-token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github-token"),
    (re.compile(r"gh[ousr]_[A-Za-z0-9]{20,}"), "github-token"),
    (re.compile(r"xox[abpr]-[A-Za-z0-9_-]{10,}"), "slack-token"),
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "google-api-key"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"), "telegram-bot-token"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*"), "bearer-token"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "jwt"),
    (re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.DOTALL,
    ), "private-key"),
)
_SECRET_NAME = r"[A-Za-z_][A-Za-z0-9_-]*(?:API_KEY|CLIENT_SECRET|PASSWORD|PASSWD|SECRET|TOKEN|KEY)[A-Za-z0-9_-]*"
_SECRET_ENV = re.compile(
    rf"(?im)(?P<name>\b{_SECRET_NAME})(?P<separator>[ \t]*=[ \t]*)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\r\n]+)"
)
_SECRET_JSON = re.compile(
    rf'(?i)(?P<prefix>"(?P<name>{_SECRET_NAME})"\s*:\s*")'
    r'(?P<value>(?:\\.|[^"\\])*)(?P<suffix>")'
)
_SECRET_NAME_ONLY = re.compile(rf"^(?:{_SECRET_NAME})$", re.IGNORECASE)


def _best_effort(func):
    """Trace-Ausfälle bleiben im Debug-Log und geben den Daemon frei."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            log.debug("Forge-Spuren-%s fehlgeschlagen (%s)", func.__name__, type(exc).__name__)
            return None
    return wrapped


def _aktiv() -> bool:
    return os.environ.get("FORGE_SPUREN", "1") != "0"


def aktiv() -> bool:
    """Ob optionale Forge-Spuren aktuell eingeschaltet sind."""
    return _aktiv()


def _tracelog_bin() -> str:
    return str(Path(os.environ.get("TRACELOG_BIN", "~/.local/bin/tracelog")).expanduser())


def _cli_vorhanden(cli: str | None = None) -> bool:
    try:
        return Path(cli or _tracelog_bin()).is_file()
    except (OSError, ValueError):
        return False


def bereit() -> bool:
    """Ob ein Trace ohne unnötige Vorarbeit an tracelog übergeben werden kann."""
    return _aktiv() and _cli_vorhanden()


def _state_path() -> Path:
    override = os.environ.get("FORGE_SPUREN_STATE") or os.environ.get("FORGE_SPUREN_STATE_PATH")
    return Path(override).expanduser() if override else Path.home() / ".local/share/forge/spuren_state.json"


def _ensure_state_cache() -> None:
    """Leert den Prozesscache, wenn Tests oder Aufrufer einen anderen State wählen."""
    global _STATE_CACHE_PATH
    path = str(_state_path().absolute())
    if _STATE_CACHE_PATH != path:
        _LAST_RECORD_IDS.clear()
        _STATE_CACHE_PATH = path


def _leerer_state() -> dict[str, Any]:
    return {"last_record_id": {}, "attempts": {}, "backfill_journal_ids": []}


def _lade_state() -> dict[str, Any]:
    try:
        state = json.loads(_state_path().read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return _leerer_state()
    except (OSError, ValueError, TypeError):
        return _leerer_state()
    state.setdefault("last_record_id", {})
    state.setdefault("attempts", {})
    state.setdefault("backfill_journal_ids", [])
    if not isinstance(state["last_record_id"], dict):
        state["last_record_id"] = {}
    if not isinstance(state["attempts"], dict):
        state["attempts"] = {}
    if not isinstance(state["backfill_journal_ids"], list):
        state["backfill_journal_ids"] = []
    return state


def _speichere_state(state: dict[str, Any]) -> None:
    path = _state_path()
    temp_name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as datei:
            temp_name = datei.name
            json.dump(state, datei, ensure_ascii=False, separators=(",", ":"))
            datei.write("\n")
        os.replace(temp_name, path)
    except Exception as exc:  # State-Ausfälle dürfen die Forge ebenfalls nicht stoppen.
        log.debug("Forge-Spuren-State nicht speicherbar (%s)", type(exc).__name__)
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass


def _task_key(task_id: int | str | None) -> str:
    return "daemon" if task_id is None else str(task_id)


def letzter_record(task_id: int | str | None) -> str | None:
    """Die zuletzt gespeicherte Trace-ID eines Tasks, auch nach Requeue."""
    with _STATE_LOCK:
        _ensure_state_cache()
        key = _task_key(task_id)
        value = _lade_state()["last_record_id"].get(key) or _LAST_RECORD_IDS.get(key)
        if value:
            _LAST_RECORD_IDS[key] = value
    return value if isinstance(value, str) and value else None


@lru_cache(maxsize=1)
def _konkrete_secret_werte() -> tuple[str, ...]:
    """Lädt nur Werte zur stillen Ersetzung; kein Wert gelangt in Logs."""
    dateien = (
        Path.home() / ".config/ai-keys.env",
        Path.home() / "Mantis/.env",
    )
    werte: set[str] = set()
    for pfad in dateien:
        try:
            zeilen = pfad.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for zeile in zeilen:
            treffer = re.match(r"^\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=\s*(.*?)\s*$", zeile)
            if not treffer:
                continue
            rohwert = treffer.group(1).strip()
            if rohwert.startswith(("\"", "'")):
                anfang = rohwert[0]
                ende = next((i for i, zeichen in enumerate(rohwert[1:], 1)
                             if zeichen == anfang and (i == 1 or rohwert[i - 1] != "\\")), len(rohwert))
                wert = rohwert[1:ende]
            else:
                wert = rohwert.split("#", 1)[0].rstrip()
            if len(wert) >= 8:
                werte.add(wert)
    return tuple(sorted(werte, key=len, reverse=True))


def _redact(text: str) -> str:
    for pattern, name in _REDACTIONS:
        text = pattern.sub(f"«REDACTED:{name}»", text)

    def env_replacement(match: re.Match) -> str:
        return f"{match.group('name')}{match.group('separator')}«REDACTED:{match.group('name')}»"

    text = _SECRET_ENV.sub(env_replacement, text)

    def json_replacement(match: re.Match) -> str:
        name = match.group("name")
        return f'{match.group("prefix")}«REDACTED:{name}»{match.group("suffix")}'

    text = _SECRET_JSON.sub(json_replacement, text)
    for secret in _konkrete_secret_werte():
        text = text.replace(secret, "«REDACTED:configured-secret»")
    return text


def _redact_obj(value: Any) -> Any:
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            name = str(key)
            redacted_key = _redact(name)
            if _SECRET_NAME_ONLY.fullmatch(name):
                redacted[redacted_key] = f"«REDACTED:{name}»"
            else:
                redacted[redacted_key] = _redact_obj(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact_obj(item) for item in value]
    return value


def _id_aus_ausgabe(output: str) -> str | None:
    zeilen = [zeile.strip() for zeile in output.splitlines() if zeile.strip()]
    if not zeilen:
        return None
    try:
        return uuid.UUID(zeilen[-1]).hex
    except (ValueError, AttributeError):
        return None


def _versuch_erhoehen(state: dict[str, Any], task_key: str, stage: str) -> int:
    schluessel = f"{task_key}:{stage}"
    try:
        versuch = int(state["attempts"].get(schluessel, 0)) + 1
    except (TypeError, ValueError):
        versuch = 1
    state["attempts"][schluessel] = versuch
    return versuch


@_best_effort
def record(
    task_id: int | str | None,
    kind: str,
    *,
    problem: str | None = None,
    source: str = "forge",
    parent: str | None = None,
    input: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    cost: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    blobs: dict[str, str | bytes | Path] | None = None,
    note: str | None = None,
) -> str | None:
    """Sendet einen Record per CLI und gibt bei Erfolg seine UUID4-ID zurück."""
    if not _aktiv():
        return None
    cli = _tracelog_bin()
    if not _cli_vorhanden(cli):
        log.debug("Forge-Spuren-Record ausgelassen (tracelog fehlt)")
        return None
    key = _task_key(task_id)
    blob_values = blobs or {}
    with _STATE_LOCK:
        _ensure_state_cache()
        state = _lade_state()
        vorheriger = parent if parent is not None else (
            state["last_record_id"].get(key) or _LAST_RECORD_IDS.get(key)
        )
        payload_input = dict(input or {})
        if kind == "stage":
            stufe = str(payload_input.get("stufe") or payload_input.get("stage") or "unbekannt")
            payload_input.setdefault("versuch", _versuch_erhoehen(state, key, stufe))
            _speichere_state(state)

    args = [cli, "record", "--source", _redact(source), "--kind", _redact(kind),
            "--problem", _redact(problem or (
                f"forge:task-{task_id}" if task_id is not None else "forge:daemon"
            ))]
    if vorheriger:
        args += ["--parent", str(vorheriger)]
    cost_data: dict[str, Any] = {
        "seconds": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cache_read": 0,
        "usd": None,
    }
    cost_data.update(cost or {})
    result_data: dict[str, Any] = {"status": "unknown", "score": None, "metrics": {}}
    result_data.update(result or {})
    result_data.setdefault("status", "unknown")
    result_data.setdefault("score", None)
    if not isinstance(result_data.get("metrics"), dict):
        result_data["metrics"] = {}
    payload: dict[str, Any] = {
        "input": _redact_obj(payload_input),
        "output": _redact_obj(output or {}),
        "cost": _redact_obj(cost_data),
        "result": _redact_obj(result_data),
    }
    if note:
        payload["note"] = _redact(note)

    try:
        with tempfile.TemporaryDirectory(prefix="forge-spuren-") as temp_dir:
            for index, (name, value) in enumerate(blob_values.items()):
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                    continue
                if isinstance(value, Path):
                    text = value.read_text(encoding="utf-8", errors="replace")
                elif isinstance(value, bytes):
                    text = value.decode("utf-8", errors="replace")
                else:
                    text = str(value)
                blob_path = Path(temp_dir) / f"{index}-{name}.txt"
                blob_path.write_text(_redact(text), encoding="utf-8")
                args += ["--blob", f"{name}={blob_path}"]
            finished = subprocess.run(
                args,
                input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                capture_output=True,
                text=True,
                timeout=_TRACE_TIMEOUT,
            )
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        log.debug("Forge-Spuren-Record fehlgeschlagen (%s)", type(exc).__name__)
        return None
    except Exception as exc:
        log.debug("Forge-Spuren-Record fehlgeschlagen (%s)", type(exc).__name__)
        return None

    if finished.returncode != 0:
        log.debug("Forge-Spuren-Record fehlgeschlagen (Exit %s)", finished.returncode)
        return None
    trace_id = _id_aus_ausgabe(finished.stdout or "")
    if trace_id is None:
        log.debug("Forge-Spuren-Record lieferte keine gültige Record-ID")
        return None
    with _STATE_LOCK:
        _ensure_state_cache()
        state = _lade_state()
        state["last_record_id"][key] = trace_id
        _LAST_RECORD_IDS[key] = trace_id
        _speichere_state(state)
    return trace_id


@_best_effort
def verdict(
    task_id: int | str | None,
    status: str,
    *,
    source: str = "forge",
    note: str | None = None,
    score: float | None = None,
) -> str | None:
    """Hängt ein unveränderliches Urteil an den letzten Task-Record."""
    if not _aktiv():
        return None
    cli = _tracelog_bin()
    if not _cli_vorhanden(cli):
        log.debug("Forge-Spuren-Verdict ausgelassen (tracelog fehlt)")
        return None
    key = _task_key(task_id)
    with _STATE_LOCK:
        _ensure_state_cache()
        state = _lade_state()
        parent = state["last_record_id"].get(key) or _LAST_RECORD_IDS.get(key)
    if not parent:
        parent = record(
            task_id,
            "decision",
            input={"event": "end_state"},
            result={"status": "unknown", "score": None, "metrics": {}},
        )
    if not parent:
        return None
    args = [cli, "verdict", str(parent), status, "--source", source]
    if note:
        args += ["--note", _redact(note)]
    if score is not None:
        args += ["--score", str(score)]
    try:
        finished = subprocess.run(args, capture_output=True, text=True, timeout=_TRACE_TIMEOUT)
    except Exception as exc:
        log.debug("Forge-Spuren-Verdict fehlgeschlagen (%s)", type(exc).__name__)
        return None
    if finished.returncode != 0:
        log.debug("Forge-Spuren-Verdict fehlgeschlagen (Exit %s)", finished.returncode)
        return None
    trace_id = _id_aus_ausgabe(finished.stdout or "")
    if trace_id:
        with _STATE_LOCK:
            _ensure_state_cache()
            state = _lade_state()
            state["last_record_id"][key] = trace_id
            _LAST_RECORD_IDS[key] = trace_id
            _speichere_state(state)
    return trace_id


def _isoformat(value: Any) -> str | None:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value) if value is not None else None


def backfill(*, dry_run: bool = False) -> int:
    """Überführt Forge-Journal-Zeilen einmalig; wiederholte Läufe sind idempotent."""
    if not _aktiv():
        return 0
    try:
        tasks = db.query("SELECT * FROM forge_tasks ORDER BY id ASC")
        journal_rows = db.query("SELECT * FROM forge_journal ORDER BY id ASC")
    except Exception as exc:
        log.debug("Forge-Spuren-Backfill konnte die DB nicht lesen (%s)", type(exc).__name__)
        return 0
    task_map = {str(task["id"]): task for task in tasks if task.get("id") is not None}
    with _STATE_LOCK:
        erledigt = {str(value) for value in _lade_state()["backfill_journal_ids"]}
    ausstehend = [row for row in journal_rows if row.get("id") is not None and str(row["id"]) not in erledigt]
    if dry_run:
        return len(ausstehend)

    geschrieben = 0
    for row in ausstehend:
        task_id = row.get("task_id")
        task = task_map.get(str(task_id), {}) if task_id is not None else {}
        trace_id = record(
            task_id,
            "journal",
            input={
                "journal_id": row["id"],
                "original_ts": _isoformat(row.get("ts")),
                "original_kind": row.get("kind"),
                "task": {
                    key: task.get(key)
                    for key in ("title", "source", "state", "priority")
                    if task.get(key) is not None
                },
            },
            output={"message": row.get("message") or ""},
            cost={
                "tokens_in": int(row.get("tokens_in") or 0),
                "tokens_out": int(row.get("tokens_out") or 0),
                "cache_read": int(row.get("cache_read") or 0),
            },
            result={"status": "ok" if row.get("kind") in {"stage_done", "gate_pass", "merged"} else "event"},
        )
        if trace_id is None:
            continue
        with _STATE_LOCK:
            state = _lade_state()
            state["backfill_journal_ids"].append(str(row["id"]))
            _speichere_state(state)
        geschrieben += 1
    return geschrieben


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m forge.spuren")
    commands = parser.add_subparsers(dest="command", required=True)
    backfill_parser = commands.add_parser("backfill", help="Forge-Journal nach tracelog übertragen")
    backfill_parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "backfill":
        count = backfill(dry_run=args.dry_run)
        prefix = "würden geschrieben" if args.dry_run else "geschrieben"
        print(f"Forge-Spuren-Backfill: {count} Records {prefix}.")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
