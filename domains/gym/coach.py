"""Gym-Chat-Use-Cases: Klassifikation, Bestätigung und knappe Antworten."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from core import db
from domains import fitness
from domains.gym import session
from domains.gym.aliases import (
    builtin_aliases, is_pullup_name, load_aliases, normalize_alias, resolve_alias,
)
from domains.gym.parser import ParsedLog, ParsedSet, has_non_exercise_start, parse_set_message
from domains.gym.progression import estimate_1rm


@dataclass(frozen=True)
class GymIntent:
    action: str
    parsed: ParsedLog | None = None
    payload: dict | None = None


@dataclass(frozen=True)
class GymResult:
    text: str
    action: str
    pending: dict | None = None


def _parsed_payload(parsed: ParsedLog) -> dict:
    return {
        "exercise": parsed.exercise,
        "sets": [{
            "weight_kg": item.weight_kg,
            "reps": item.reps,
            "is_warmup": item.is_warmup,
        } for item in parsed.sets],
        "equipment": parsed.equipment,
        "load_mode": parsed.load_mode,
        "known": parsed.known,
        "alias": parsed.alias,
        "fuzzy": parsed.fuzzy,
        "ambiguous": list(parsed.ambiguous),
    }


def _payload_parsed(payload: dict) -> ParsedLog | None:
    try:
        exercise = payload.get("exercise")
        sets = tuple(
            ParsedSet(
                float(item["weight_kg"]) if item.get("weight_kg") is not None else None,
                int(item["reps"]),
                bool(item.get("is_warmup")),
            )
            for item in payload["sets"]
        )
        if not sets or any(not 1 <= item.reps <= 30 for item in sets):
            return None
        return ParsedLog(
            exercise=exercise, sets=sets, equipment=payload.get("equipment"),
            load_mode=payload.get("load_mode") or "external",
            known=bool(payload.get("known")), alias=payload.get("alias"),
            fuzzy=bool(payload.get("fuzzy")),
            ambiguous=tuple(payload.get("ambiguous") or ()),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _timestamp(value: str | datetime | None) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _confirmation(text: str) -> str | None:
    value = normalize_alias(text).strip(" .,!?")
    if value in {"ja", "jo", "jap", "jop", "genau", "richtig", "passt", "yes", "yep"}:
        return "yes"
    if value in {"nein", "nee", "no", "ne", "stopp", "abbrechen"}:
        return "no"
    return None


def _format_weight(value: float | None) -> str:
    if value is None:
        return "Körpergewicht"
    return f"{value:g}".replace(".", ",") + " kg"


def _format_reps(values: list[int] | tuple[int, ...]) -> str:
    return "/".join(str(value) for value in values)


def _history_target(sets: list[dict]) -> str:
    values = [int(row.get("reps") or 0) for row in sets]
    return _format_reps(values)


class GymCoach:
    """Ein Nachrichten-Use-Case ohne Agentenlauf auf dem Erfolgsweg."""

    def _latest_pending(self) -> dict | None:
        try:
            rows = db.query(
                """SELECT meta FROM chat_messages
                   WHERE role='assistant' ORDER BY id DESC LIMIT 1"""
            )
        except Exception:
            return None
        if not rows:
            return None
        meta = rows[0].get("meta") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                return None
        pending = meta.get("gym_pending") if isinstance(meta, dict) else None
        if isinstance(pending, dict) and pending.get("resolved"):
            return None
        return pending if isinstance(pending, dict) else None

    def _pending_intent(self, text: str, pending: dict | None, now: datetime) -> GymIntent | None:
        if not pending:
            return None
        choice = _confirmation(text)
        kind = pending.get("kind")
        if kind == "split":
            if not self._split_sequence(text):
                return None
            expiry = _timestamp(pending.get("expires_at"))
            if expiry and now >= expiry:
                return GymIntent("pending_expired")
            return GymIntent("split_answer", payload={**pending, "answer": text})
        if not choice:
            candidate = pending.get("candidate") or {}
            selected = resolve_alias(text, {normalize_alias(name): [name]
                                            for name in candidate.get("ambiguous", [])},
                                     allow_typo=False)[0]
            if selected:
                expiry = _timestamp(pending.get("expires_at"))
                if expiry and expiry.tzinfo is None and now.tzinfo:
                    expiry = expiry.replace(tzinfo=now.tzinfo)
                if expiry and now >= expiry:
                    return GymIntent("pending_expired")
                return GymIntent("pending_selected", payload={**pending, "selected": selected})
            return None
        expiry = _timestamp(pending.get("expires_at"))
        current = now
        if expiry and expiry.tzinfo is None and current.tzinfo:
            expiry = expiry.replace(tzinfo=current.tzinfo)
        if expiry and current >= expiry:
            return GymIntent("pending_expired")
        if choice == "no":
            return GymIntent("pending_rejected")
        return GymIntent("pending_confirmed", payload=pending)

    @staticmethod
    def _history_alias(text: str, aliases: dict[str, list[str]]) -> str | None:
        cleaned = normalize_alias(text)
        cleaned = re.sub(r"^(?:wie lief|wie war|verlauf von|verlauf|zeig mir)", "", cleaned).strip()
        cleaned = re.sub(r"(?:verlauf|historie|fortschritt)$", "", cleaned).strip()
        exercise, _ambiguous, _fuzzy = resolve_alias(cleaned, aliases, allow_typo=False)
        return exercise

    @staticmethod
    def _correction_intent(text: str, aliases: dict[str, list[str]]) -> GymIntent | None:
        normalized = normalize_alias(text)
        match = re.search(
            r"(?:\bfalsch\b.*)?\bletzter satz\s+(\d{1,2})\s+nicht\s+(\d{1,2})\b",
            normalized,
        )
        if match and all(1 <= int(value) <= 30 for value in match.groups()):
            return GymIntent(
                "correct_reps",
                payload={"reps": int(match.group(1)), "expected_reps": int(match.group(2))},
            )
        if re.search(r"\b(?:loesch|loesche|entferne)\s+(?:bitte\s+)?(?:den\s+)?letzten satz\b", normalized):
            return GymIntent("delete_last_set")
        match = re.match(r"(.+?)\s+war\s+([+]?\d+(?:[.,]\d+)?)\s*(?:kg)?$", text.strip(), re.I)
        if match:
            name, weight = match.groups()
            exercise, _ambiguous, _fuzzy = resolve_alias(name, aliases)
            try:
                value = float(weight.replace(",", "."))
            except ValueError:
                return None
            if exercise and value > 0:
                return GymIntent("correct_load", payload={"exercise": exercise, "weight_kg": value})
        return None

    @staticmethod
    def _needs_curl_equipment_clarification(parsed: ParsedLog) -> bool:
        if parsed.equipment:
            return False
        return any(
            "curl" in normalize_alias(value or "")
            for value in (parsed.alias, parsed.exercise)
        )

    @staticmethod
    def _strong_training_intent(text: str, aliases: dict[str, list[str]]) -> bool:
        normalized = normalize_alias(text)
        if has_non_exercise_start(text):
            return False
        if GymCoach._looks_like_time_or_date(text):
            return False
        if any(term in normalized for term in (
            "pound", " lb", "umrechnen", "wie viel", "wieviel", "gelaufen",
            "laufen", "joggen", "laufband", "kalorie", "kcal", "stunden",
            "ich bin", "ich wiege",
        )) or re.search(r"\bmal\b", normalized):
            return False
        has_marker = bool(re.search(r"\b(?:kg|wdh|wiederholungen?|satz|saetze|x)\b|×", text, re.I))
        numbers = re.findall(r"[+]?[0-9]+(?:[.,][0-9]+)?", text)
        known_name = any(normalized.startswith(alias + " ") or normalized == alias for alias in aliases)
        name_match = re.match(r"^\s*(.*?)\s*[+]?\d", text)
        has_exercise_name = bool(name_match and normalize_alias(name_match.group(1)))
        values = []
        for raw in numbers:
            try:
                values.append(float(raw.lstrip("+").replace(",", ".")))
            except ValueError:
                continue
        if known_name and has_marker:
            return (
                len(values) >= 2 and 0 < values[0] <= 10000
                and all(1 <= value <= 30 for value in values[1:])
            )
        return (
            has_exercise_name and len(values) >= 3 and values[0] > 30
            and all(1 <= value <= 30 for value in values[1:])
        )

    @staticmethod
    def _looks_like_time_or_date(text: str) -> bool:
        raw_values = re.findall(r"(?<![\w.])[+]?[0-9]+(?:[.,][0-9]+)?(?![\d.])", text)
        try:
            values = [float(value.lstrip("+").replace(",", ".")) for value in raw_values]
        except ValueError:
            return False
        if len(values) >= 3 and values[0] >= 1900:
            if 1 <= values[1] <= 12 and 1 <= values[2] <= 31:
                return True
        if len(values) < 2:
            return False
        pairs = list(zip(values, values[1:]))
        if len(values) == 2:
            return any(
                0 <= first <= 24 and 0 <= second <= 59
                or 1 <= first <= 31 and 1 <= second <= 12
                for first, second in pairs
            )
        if all(0 <= value <= 31 for value in values):
            return any(
                0 <= first <= 24 and 0 <= second <= 59
                or 1 <= first <= 31 and 1 <= second <= 12
                for first, second in pairs
            )
        return False

    def classify(self, text: str, now: datetime | None = None) -> GymIntent | None:
        now = session.local_now(now)
        normalized = normalize_alias(text).strip(" .,!?")
        static_aliases = builtin_aliases()
        pending_reply = bool(
            _confirmation(text)
            or self._split_sequence(text)
            or normalized in static_aliases
        )
        if pending_reply:
            pending_intent = self._pending_intent(text, self._latest_pending(), now)
            if pending_intent:
                return pending_intent

        if normalized in {"training fertig", "training ist fertig", "workout fertig", "gym fertig"}:
            return GymIntent("finish")
        if normalized == "fertig":
            try:
                if session.open_session(now):
                    return GymIntent("finish")
            except Exception:
                return None

        if re.search(r"\bwas mach ich heute\b", normalized):
            explicit_gym = any(word in normalized for word in ("gym", "training", "trainier", "workout", "push", "pull", "beine"))
            try:
                active = session.open_session(now)
            except Exception:
                active = None
            if active or explicit_gym:
                return GymIntent("next_workout")

        history_marker = any(word in normalized for word in ("verlauf", "historie", "wie lief", "wie war"))
        if history_marker:
            aliases = load_aliases()
            exercise = self._history_alias(text, aliases)
            if exercise:
                try:
                    session.open_session(now)
                except Exception:
                    pass
                return GymIntent("history", payload={"exercise": exercise})

        # Gewöhnliche Sätze ohne Zahlen oder explizite Gym-Aktion gehen direkt
        # weiter, ohne Alias- oder Sessionabfragen an die Datenbank.
        has_numbers = bool(re.search(r"\d", text))
        has_correction = any(word in normalized for word in ("falsch", "loesch", "loesche", "war"))
        if not has_numbers and not has_correction:
            return None

        aliases = load_aliases()
        correction = self._correction_intent(text, aliases)
        if correction:
            return correction
        parsed = parse_set_message(text, aliases)
        if parsed:
            if parsed.ambiguous:
                return GymIntent("ambiguous_exercise", parsed=parsed)
            if parsed.known:
                if self._needs_curl_equipment_clarification(parsed):
                    return GymIntent("clarify_equipment", parsed=parsed)
                return GymIntent("set_logged", parsed=parsed)
            explicit_gym = bool(
                parsed.equipment
                or re.search(r"\b(?:kg|kilo|wdh|wiederholungen?|reps?|sätze?|saetze?)\b", text, re.I)
            )
            if not explicit_gym and self._looks_like_time_or_date(text):
                return None
            if explicit_gym:
                return GymIntent("confirm_new_exercise", parsed=parsed)
            try:
                active_session = session.open_session(now)
            except Exception:
                active_session = None
            if active_session or self._strong_training_intent(text, aliases):
                return GymIntent("confirm_new_exercise", parsed=parsed)
            return None

        if self._strong_training_intent(text, aliases):
            return GymIntent("parser_fallback", payload={"text": text})
        return None

    @staticmethod
    def _pending(kind: str, candidate: dict, now: datetime) -> dict:
        current = session.local_now(now)
        return {
            "kind": kind,
            "candidate": candidate,
            "expires_at": (current + timedelta(minutes=10)).isoformat(),
        }

    async def process(self, intent: GymIntent, *, llm_fallback=None,
                      now: datetime | None = None) -> GymResult:
        now = session.local_now(now)
        action = intent.action
        if action == "pending_expired":
            return GymResult("⏱️ Die Bestätigung ist abgelaufen. Sende den Satz bitte erneut.", action)
        if action == "pending_rejected":
            return GymResult("Alles klar, verworfen. ❌", action)
        if action == "confirm_new_exercise":
            parsed = intent.parsed
            candidate = _parsed_payload(parsed)
            pending = self._pending("new_exercise", candidate, now)
            return GymResult(
                f"Neue Übung ‚{parsed.exercise}‘ anlegen? Ich speichere sie und übernehme den Satz nach deiner Bestätigung.",
                action, pending,
            )
        if action == "ambiguous_exercise":
            parsed = intent.parsed
            candidate = _parsed_payload(parsed)
            pending = self._pending("ambiguous", candidate, now)
            choices = " oder ".join(parsed.ambiguous)
            return GymResult(f"Meinst du {choices}? Schreib den passenden Übungsnamen.", action, pending)
        if action == "clarify_equipment":
            return GymResult(
                "❓ Meinst du Kurzhantel (KH), SZ-Stange oder Kabel? "
                "Sende den Satz bitte noch einmal mit der Variante, z. B. „KH Curls 15 4x10“.",
                action,
            )
        if action == "parser_fallback":
            if llm_fallback is None:
                return GymResult("❓ Schreib den Satz bitte eindeutiger, z. B. Bank 80 8 8.", action)
            aliases = load_aliases()
            try:
                raw = await llm_fallback((intent.payload or {}).get("text", ""), aliases)
                candidate = self._candidate_from_model(raw, aliases)
            except Exception:
                candidate = None
            if candidate is None:
                return GymResult("❓ Ich konnte den Satz nicht sicher lesen. Schreib ihn bitte eindeutiger.", action)
            parsed = _payload_parsed(candidate)
            if parsed is None:
                return GymResult("❓ Ich konnte den Satz nicht sicher lesen. Schreib ihn bitte eindeutiger.", action)
            if parsed.ambiguous:
                pending = self._pending("ambiguous", candidate, now)
                choices = " oder ".join(parsed.ambiguous)
                reps = _history_target(candidate["sets"])
                return GymResult(
                    f"So verstanden: {reps} Wiederholungen. Meinst du {choices}? Schreib den Übungsnamen.",
                    action, pending,
                )
            if not parsed.exercise:
                return GymResult("❓ Ich konnte keine eindeutige Übung erkennen. Schreib den Namen bitte dazu.", action)
            kind = "candidate" if parsed.known else "new_exercise"
            pending = self._pending(kind, candidate, now)
            reps = _history_target(candidate["sets"])
            weight = _format_weight(candidate["sets"][0].get("weight_kg"))
            if kind == "new_exercise":
                reply = f"So verstanden: {parsed.exercise}, {weight} · {reps}. Neue Übung anlegen und speichern? Antworte ja oder nein."
            else:
                reply = f"So verstanden: {parsed.exercise}, {weight} · {reps}. Passt? Antworte ja oder nein."
            return GymResult(reply, action, pending)
        if action in {"pending_confirmed", "pending_selected"}:
            pending = intent.payload or {}
            candidate = pending.get("candidate") or {}
            if pending.get("selected"):
                candidate = {**candidate, "exercise": pending["selected"], "known": True, "ambiguous": []}
            parsed = _payload_parsed(candidate)
            if parsed is None or parsed.exercise is None:
                return GymResult("❓ Sende den Satz bitte noch einmal in einer klaren Form.", action)
            return await self._save_set(parsed, now, allow_create=not parsed.known)
        if action == "set_logged" and intent.parsed:
            return await self._save_set(intent.parsed, now, allow_create=False)
        if action == "finish":
            result = session.finish(now)
            if not result:
                return GymResult("❓ Keine offene Gym-Session.", action)
            exercises = ", ".join(result["exercises"]) or "keine Übungen"
            volume = f"{result['volume_kg']:g}".replace(".", ",")
            text = (
                f"✅ Training fertig · {result['duration_min']} Min · {result['exercise_count']} Übungen · "
                f"{result['set_count']} Sätze · {volume} kg Volumen · {exercises}"
            )
            if result.get("timed_out"):
                text = "⏱️ " + text
            if result.get("prs"):
                text += " · 🏆 " + ", ".join(result["prs"])
            return GymResult(text, action)
        if action == "history":
            return self._history(intent.payload["exercise"])
        if action.startswith("correct_") or action == "delete_last_set":
            return self._correct(intent, now)
        if action == "next_workout":
            return self._next_workout(now)
        if action == "split_answer":
            return self._save_split_answer(intent.payload, now)
        return GymResult("❓ Das habe ich beim Training nicht verstanden.", action)

    async def _save_set(self, parsed: ParsedLog, now: datetime, *, allow_create: bool) -> GymResult:
        try:
            result = session.add_sets(parsed, allow_create=allow_create, now=now)
        except ValueError:
            return GymResult("❓ Ich brauche erst deine Bestätigung für die neue Übung.", "set_logged")
        except Exception:
            return GymResult("❌ Satz konnte nicht gespeichert werden. Bitte sende ihn noch einmal.", "set_logged")
        return GymResult(self._log_reply(result), "set_logged")

    @staticmethod
    def _log_reply(result: dict) -> str:
        current = result["sets"]
        weights = [row.get("weight_kg") for row in current]
        reps = [int(row["reps"]) for row in current]
        metadata = result.get("metadata") or {}
        weight_unit = (
            " je Hantel"
            if metadata.get("equipment") == "dumbbell" and metadata.get("dumbbell_pair", True)
            else ""
        )
        same_weight = len(set(weights)) == 1
        if same_weight:
            summary = f"{_format_weight(weights[0])}{weight_unit} · {_format_reps(reps)}"
        else:
            summary = " · ".join(
                f"{_format_weight(row.get('weight_kg'))}{weight_unit} × {row['reps']}"
                for row in current
            )
        text = f"✅ {result['exercise']} {summary}"
        previous = result.get("previous_sets") or []
        if previous:
            prior_weight = previous[0].get("weight_kg")
            text += (
                f" · letztes Mal {_format_weight(prior_weight)}{weight_unit} "
                f"{_history_target(previous)}"
            )
        progression = result.get("progression") or {}
        if progression.get("target_reps"):
            text += (
                f" · nächstes Mal: {_format_weight(progression.get('weight_kg'))}{weight_unit}, "
                f"Ziel {_format_reps(progression['target_reps'])}"
            )
        records = result.get("records") or {}
        if any(records.values()):
            labels = [label for key, label in (("weight", "Gewicht"), ("reps", "Wdh."), ("e1rm", "e1RM"))
                      if records.get(key)]
            text += " · 🏆 " + "/".join(labels)
        return text

    @staticmethod
    def _candidate_from_model(raw: Any, aliases: dict[str, list[str]]) -> dict | None:
        if isinstance(raw, tuple):
            raw = raw[0]
        if isinstance(raw, dict):
            data = raw
        else:
            text = str(raw or "").strip()
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                return None
            try:
                data = json.loads(match.group())
            except ValueError:
                return None
        exercise = str(data.get("exercise") or "").strip()
        raw_sets = data.get("sets")
        if not exercise or not isinstance(raw_sets, list) or not 1 <= len(raw_sets) <= 20:
            return None
        parsed_sets = []
        for item in raw_sets:
            if not isinstance(item, dict):
                return None
            try:
                raw_reps = item.get("reps")
                if isinstance(raw_reps, bool):
                    return None
                if isinstance(raw_reps, int):
                    reps = raw_reps
                elif isinstance(raw_reps, str) and re.fullmatch(r"\d+", raw_reps.strip()):
                    reps = int(raw_reps)
                else:
                    return None
                weight = item.get("weight_kg")
                if isinstance(weight, bool):
                    return None
                weight = float(weight) if weight is not None else None
            except (TypeError, ValueError):
                return None
            if (
                not 1 <= reps <= 30
                or weight is not None and (not math.isfinite(weight) or not 0 < weight <= 10000)
            ):
                return None
            parsed_sets.append({"weight_kg": weight, "reps": reps})
        canonical, ambiguous, fuzzy = resolve_alias(exercise, aliases)
        if not ambiguous and not is_pullup_name(canonical) and any(
            item["weight_kg"] is None for item in parsed_sets
        ):
            return None
        load_mode = data.get("load_mode") or "external"
        if is_pullup_name(canonical):
            load_mode = "added" if any(item["weight_kg"] is not None for item in parsed_sets) else "bodyweight"
        return _parsed_payload(ParsedLog(
            exercise=canonical or (None if ambiguous else exercise),
            sets=tuple(ParsedSet(**item) for item in parsed_sets),
            equipment=data.get("equipment"), load_mode=load_mode,
            known=bool(canonical), alias=exercise, fuzzy=fuzzy, ambiguous=ambiguous,
        ))

    @staticmethod
    def _history(exercise_name: str) -> GymResult:
        sessions = session.exercise_history(exercise_name, limit=5)
        if not sessions:
            return GymResult(f"ℹ️ Für {exercise_name} habe ich noch keinen Verlauf.", "history")
        rows = []
        e1rms = []
        for workout in sessions:
            sets = workout["sets"]
            date_value = workout.get("date")
            if hasattr(date_value, "strftime"):
                date_text = date_value.strftime("%d.%m.")
            else:
                try:
                    date_text = date.fromisoformat(str(date_value)[:10]).strftime("%d.%m.")
                except ValueError:
                    date_text = str(date_value)[:10]
            rendered = []
            session_e1rms = []
            for item in sets:
                weight = _format_weight(item.get("weight_kg"))
                rendered.append(f"{weight}×{item.get('reps')}")
                estimate = None if item.get("load_mode") in {"added", "bodyweight"} else estimate_1rm(
                    item.get("weight_kg"), item.get("reps")
                )
                if estimate is not None:
                    session_e1rms.append(estimate)
            if session_e1rms:
                e1rms.append((workout.get("created_at") or date_value, max(session_e1rms)))
            rows.append(f"{date_text}: {'/'.join(rendered)}")
        trend = ""
        if e1rms:
            ordered = sorted(e1rms, key=lambda item: str(item[0]))
            delta = ordered[-1][1] - ordered[0][1]
            best = max(value for _date, value in e1rms)
            trend = f" · e1RM {'+' if delta >= 0 else ''}{delta:.1f} kg · Bestwert {best:.1f} kg"
        return GymResult(f"📈 {exercise_name} · {' | '.join(rows)}{trend}", "history")

    def _correct(self, intent: GymIntent, now: datetime) -> GymResult:
        payload = intent.payload or {}
        try:
            if intent.action == "correct_reps":
                result = session.correct_last_reps(
                    payload["reps"], now, expected_reps=payload.get("expected_reps")
                )
                if result and result.get("conflict"):
                    return GymResult(
                        f"❓ Der letzte Satz hat {result['reps']} Wdh., nicht "
                        f"{payload['expected_reps']}. Welche Zahl stimmt?",
                        intent.action,
                    )
                label = f"jetzt {payload['reps']} Wdh."
            elif intent.action == "delete_last_set":
                result = session.delete_last_set(now)
                if result and result.get("empty"):
                    return GymResult("✅ Letzter Satz gelöscht; das leere Workout ist entfernt.", intent.action)
                label = "letzter Satz gelöscht"
            else:
                result = session.correct_last_load(payload["exercise"], payload["weight_kg"], now)
                label = f"{payload['exercise']} jetzt {_format_weight(payload['weight_kg'])}"
        except Exception:
            return GymResult("❌ Korrektur fehlgeschlagen. Es wurde nichts geändert.", intent.action)
        if not result:
            return GymResult("❓ Keine passende offene Session; ich ändere kein früheres Workout.", intent.action)
        text = f"✅ Korrigiert · {label}."
        progression = result.get("progression") or {}
        if progression.get("target_reps"):
            text += (
                f" Nächstes Mal: {_format_weight(progression.get('weight_kg'))}, "
                f"Ziel {_format_reps(progression['target_reps'])}."
            )
        records = result.get("records") or {}
        if any(records.values()):
            text += " 🏆 PR aktualisiert."
        return GymResult(text, intent.action)

    def _next_workout(self, now: datetime) -> GymResult:
        split = db.get_setting("gym_split")
        history = session.workout_exercise_history(limit=200)
        workouts: dict[int, dict] = {}
        for row in history:
            if row.get("is_warmup"):
                continue
            entry = workouts.setdefault(row["workout_id"], {
                "date": row.get("date"), "created_at": row.get("created_at"), "rows": [],
            })
            entry["rows"].append(row)
        ordered = sorted(
            workouts.values(),
            key=lambda item: str(item.get("created_at") or item.get("date") or date.min),
        )
        categories = [self._workout_category(item["rows"]) for item in ordered]
        categories = [item for item in categories if item]
        sequence = self._split_sequence(split) if split else []
        if sequence:
            last = categories[-1] if categories else None
            next_category = sequence[(sequence.index(last) + 1) % len(sequence)] if last in sequence else sequence[0]
            return self._next_workout_reply(next_category, history, now)
        if len(categories) >= 3 and len(set(categories)) >= 3:
            rotation = list(dict.fromkeys(categories))
            next_category = rotation[(rotation.index(categories[-1]) + 1) % len(rotation)]
            return self._next_workout_reply(next_category, history, now)
        plan_reply = self._active_plan_reply(history)
        if plan_reply:
            return plan_reply
        pending = self._pending("split", {"query": "next_workout"}, now)
        return GymResult("❓ Welchen Split nutzt du? Zum Beispiel Push/Pull/Beine oder Upper/Lower.", "next_workout", pending)

    @staticmethod
    def _workout_category(rows: list[dict]) -> str | None:
        groups = set()
        for row in rows:
            muscle = str(row.get("muscle") or "").lower()
            name = normalize_alias(row.get("exercise") or "")
            if muscle in {"chest", "shoulders"} or "trizeps" in name or "triceps" in name:
                groups.add("Push")
            elif muscle == "back" or "bizeps" in name or "biceps" in name or "klimmzug" in name:
                groups.add("Pull")
            elif muscle == "legs":
                groups.add("Beine")
        if len(groups) == 1:
            return next(iter(groups))
        if len(groups) >= 3:
            return "Ganzkörper"
        return None

    @staticmethod
    def _active_plan_reply(history: list[dict]) -> GymResult | None:
        try:
            active = fitness.active_plan()
        except Exception:
            return None
        plan = active.get("plan_json") if active else None
        if isinstance(plan, str):
            try:
                plan = json.loads(plan)
            except ValueError:
                return None
        if not isinstance(plan, dict):
            return None
        keys = [key for key in ("upperA", "lowerA", "upperB", "lowerB")
                if isinstance(plan.get(key), list) and plan[key]]
        if not keys:
            return None
        recent_workout = next((row.get("workout_id") for row in history if not row.get("is_warmup")), None)
        recent_names = {
            normalize_alias(row.get("exercise") or "") for row in history
            if row.get("workout_id") == recent_workout and not row.get("is_warmup")
        }
        best_key = None
        best_overlap = 0
        for key in keys:
            names = {normalize_alias(item.get("name") or "") for item in plan[key] if isinstance(item, dict)}
            overlap = len(names & recent_names)
            if overlap > best_overlap:
                best_key, best_overlap = key, overlap
        next_key = keys[(keys.index(best_key) + 1) % len(keys)] if best_key else keys[0]
        latest: dict[str, dict] = {}
        for row in history:
            name = normalize_alias(row.get("exercise") or "")
            if name and not row.get("is_warmup"):
                latest.setdefault(name, row)
        suggestions = []
        for item in plan[next_key]:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            name = str(item["name"])
            last = latest.get(normalize_alias(name))
            weight = last.get("weight_kg") if last and last.get("weight_kg") is not None else item.get("weight")
            target = item.get("reps") or (last.get("reps") if last else 8)
            if weight is not None:
                suggestions.append(f"{name} {_format_weight(weight)}, Ziel {target}")
        detail = " · " + "; ".join(suggestions[:3]) if suggestions else ""
        label = next_key.removesuffix("A").removesuffix("B").capitalize()
        label += " " + next_key[-1].upper()
        return GymResult(f"🏋️ Nächste Plan-Einheit: {label}{detail}", "next_workout")

    @staticmethod
    def _split_sequence(split: Any) -> list[str]:
        if isinstance(split, list):
            raw = split
        else:
            raw = re.split(r"[/,>\s]+", str(split or ""))
        labels = {"push": "Push", "pull": "Pull", "beine": "Beine", "legs": "Beine",
                  "upper": "Upper", "lower": "Lower", "ganzkörper": "Ganzkörper",
                  "ganzkoerper": "Ganzkörper", "full body": "Ganzkörper"}
        return [labels[normalize_alias(item)] for item in raw if normalize_alias(item) in labels]

    @staticmethod
    def _next_workout_reply(category: str, history: list[dict], now: datetime) -> GymResult:
        suggestions = []
        seen = set()
        for row in history:
            if row.get("is_warmup") or row.get("exercise") in seen:
                continue
            if GymCoach._workout_category([row]) != category:
                continue
            seen.add(row.get("exercise"))
            if row.get("weight_kg") is not None:
                low, high = (8, 12) if any(word in normalize_alias(row.get("exercise")) for word in ("curl", "heben", "fly")) else (6, 10)
                target = min(high, int(row.get("reps") or low) + 1)
                suggestions.append(f"{row['exercise']} {_format_weight(row['weight_kg'])}, Ziel {target}")
            if len(suggestions) == 3:
                break
        detail = " · " + "; ".join(suggestions) if suggestions else ""
        return GymResult(f"🏋️ Nächste Einheit: {category}{detail}", "next_workout")

    def _save_split_answer(self, pending: dict | None, now: datetime) -> GymResult:
        sequence = self._split_sequence(pending.get("answer") if pending else None)
        if not sequence:
            # The current answer is attached to the intent by classify below.
            return GymResult("❓ Nenne den Split bitte als Push/Pull/Beine oder Upper/Lower.", "split_answer")
        label = "/".join(sequence)
        db.set_setting("gym_split", label)
        next_category = sequence[0]
        history = session.workout_exercise_history(limit=200)
        categories_by_workout: dict[int, list[dict]] = {}
        for row in history:
            if not row.get("is_warmup"):
                categories_by_workout.setdefault(row["workout_id"], []).append(row)
        ordered = sorted(
            categories_by_workout.values(),
            key=lambda rows: str(rows[0].get("created_at") or rows[0].get("date") or ""),
        )
        categories = [self._workout_category(rows) for rows in ordered]
        categories = [item for item in categories if item]
        if categories and categories[-1] in sequence:
            next_category = sequence[(sequence.index(categories[-1]) + 1) % len(sequence)]
        reply = self._next_workout_reply(next_category, history, now)
        return GymResult(f"✅ Split gespeichert: {label} · {reply.text.removeprefix('🏋️ ')}", "split_answer")

