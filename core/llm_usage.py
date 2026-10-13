"""
LLM-Usage-Tracking — erfasst Token-Verbrauch und API-Kosten aller LLM-Calls.

record() ist fire-and-forget: Fehler werden geschluckt, das Tracking darf
niemals einen Chat-Turn brechen. Ollama-Calls werden mit Kosten 0 erfasst
(interessant für Auslastung, kostenlos im Betrieb).
"""
import asyncio
import logging

import httpx

from core.error_redaction import redact_error_text

log = logging.getLogger(__name__)
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
GENERATION_LOOKUP_TIMEOUT_S = 10
_pending_generation_lookups: set[asyncio.Task] = set()

# USD pro 1M Tokens (input, output) — Substring-Match auf den Modellnamen.
# Reihenfolge zählt: spezifischere Muster zuerst.
PRICES: list[tuple[str, float, float]] = [
    ("haiku",  1.00,  5.00),
    ("sonnet", 3.00, 15.00),
    ("opus",  15.00, 75.00),
    # Schätzwerte, nur wenn OpenRouter für die Generation keinen Preis liefert.
    ("openai/gpt-6-luna", 0.10, 0.40),
    ("google/gemini-3.1-flash-lite", 0.075, 0.30),
]


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Schätzt bekannte Modellkosten; unbekannte Modelle bleiben unbekannt."""
    m = (model or "").lower()
    for pattern, in_price, out_price in PRICES:
        if pattern in m:
            return (input_tokens * in_price + output_tokens * out_price) / 1_000_000
    return None


def _fallback_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> float | None:
    if (provider or "").lower() == "ollama":
        return 0.0
    return cost_usd(model, input_tokens, output_tokens)


def record(provider: str, model: str, input_tokens: int, output_tokens: int,
           purpose: str = "chat", actual_cost: float | None = None,
           generation_id: str | None = None, api_key: str | None = None,
           generation_client: httpx.AsyncClient | None = None) -> None:
    """Schreibt einen Usage-Eintrag. Läuft im Event-Loop asynchron (nicht blockierend),
    außerhalb davon synchron. Fehler werden nur geloggt."""
    input_count = input_tokens or 0
    output_count = output_tokens or 0
    if not input_count and not output_count and actual_cost is None and not generation_id:
        return
    try:
        from core import db
        needs_generation_lookup = (
            (provider or "").lower() == "openrouter"
            and generation_id is not None
            and actual_cost is None
        )
        cost = None if needs_generation_lookup else (
            actual_cost if actual_cost is not None
            else _fallback_cost(provider, model, input_count, output_count)
        )
        sql = ("INSERT INTO llm_usage (provider, model, purpose, input_tokens, "
               "output_tokens, cost_usd) VALUES (%s,%s,%s,%s,%s,%s)")
        params = (provider, model, purpose, input_count, output_count, cost)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            if needs_generation_lookup:
                task = loop.create_task(_record_with_generation_cost(
                    provider, model, purpose, input_count, output_count,
                    generation_id, api_key, generation_client,
                ))
                _pending_generation_lookups.add(task)
                task.add_done_callback(_generation_lookup_done)
            else:
                task = loop.create_task(db.aexecute(sql, params))
                task.add_done_callback(_log_task_error)
        else:
            if needs_generation_lookup:
                params = (
                    provider, model, purpose, input_count, output_count,
                    _fallback_cost(provider, model, input_count, output_count),
                )
            db.execute(sql, params)
    except Exception as e:
        log.debug("llm_usage.record fehlgeschlagen: %s", redact_error_text(e))


async def _record_with_generation_cost(
    provider: str,
    model: str,
    purpose: str,
    input_tokens: int,
    output_tokens: int,
    generation_id: str,
    api_key: str | None,
    client: httpx.AsyncClient | None,
) -> None:
    """Schreibt zunächst unbekannte Kosten und trägt danach Generation-Kosten nach."""
    from core import db

    insert_sql = (
        "INSERT INTO llm_usage (provider, model, purpose, input_tokens, output_tokens, cost_usd) "
        "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id"
    )
    params = (provider, model, purpose, input_tokens, output_tokens, None)
    try:
        row_id = await db.ainsert_returning(insert_sql, params)
    except Exception as exc:
        log.debug("llm_usage.insert fehlgeschlagen: %s", type(exc).__name__)
        return
    if row_id is None:
        return

    try:
        cost = await asyncio.wait_for(
            _fetch_openrouter_generation_cost(generation_id, api_key, client),
            timeout=GENERATION_LOOKUP_TIMEOUT_S,
        )
    except Exception as exc:
        log.debug("OpenRouter-Usage-Lookup fehlgeschlagen: %s", type(exc).__name__)
        cost = None
    if cost is None:
        cost = _fallback_cost(provider, model, input_tokens, output_tokens)
    if cost is None:
        return
    try:
        await db.aexecute("UPDATE llm_usage SET cost_usd = %s WHERE id = %s", (cost, row_id))
    except Exception as exc:
        log.debug("llm_usage.cost-backfill fehlgeschlagen: %s", type(exc).__name__)


def _generation_lookup_done(task: asyncio.Task) -> None:
    _pending_generation_lookups.discard(task)
    _log_task_error(task)


async def shutdown_pending_generation_lookups() -> None:
    """Bricht laufende Kosten-Nachfragen vor dem Schließen der DB sauber ab."""
    tasks = tuple(_pending_generation_lookups)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _fetch_openrouter_generation_cost(
    generation_id: str,
    api_key: str | None,
    client: httpx.AsyncClient | None = None,
) -> float | None:
    """Liest den tatsächlichen Preis einer OpenRouter-Generation; Fehler bleiben tolerant."""
    if not generation_id:
        return None
    if api_key is None:
        try:
            import config
            api_key = config.OPENROUTER_API_KEY
        except Exception:
            api_key = ""
    key = str(api_key or "").strip()
    if not key or any(char.isspace() for char in key):
        return None
    headers = {"Authorization": f"Bearer {key}"}
    try:
        if client is None:
            async with httpx.AsyncClient(base_url=OPENROUTER_BASE_URL, timeout=5) as owned_client:
                response = await owned_client.get("/generation", params={"id": generation_id}, headers=headers)
        else:
            response = await client.get(
                "/generation", params={"id": generation_id}, headers=headers, timeout=5,
            )
        response.raise_for_status()
        data = response.json().get("data") or {}
        value = data.get("total_cost")
        if value is None:
            value = data.get("cost")
        return float(value) if value is not None else None
    except Exception as exc:
        log.debug("OpenRouter-Usage-Lookup fehlgeschlagen: %s", type(exc).__name__)
        return None


def _log_task_error(task: asyncio.Task) -> None:
    exc = task.exception() if not task.cancelled() else None
    if exc:
        log.debug("llm_usage insert fehlgeschlagen: %s", redact_error_text(exc))


def summary(days: int = 30) -> dict:
    """Aggregierte Usage fürs Dashboard / api_costs-Tool."""
    from core import db
    daily = db.query(
        """
        SELECT created_at::date AS date,
               SUM(input_tokens)  AS input_tokens,
               SUM(output_tokens) AS output_tokens,
               SUM(cost_usd)      AS cost_usd
        FROM llm_usage
        WHERE created_at >= CURRENT_DATE - %s::int
        GROUP BY 1 ORDER BY 1
        """,
        (days,),
    )
    by_model = db.query(
        """
        SELECT provider, model,
               COUNT(*)           AS calls,
               SUM(input_tokens)  AS input_tokens,
               SUM(output_tokens) AS output_tokens,
               SUM(cost_usd)      AS cost_usd
        FROM llm_usage
        WHERE created_at >= CURRENT_DATE - %s::int
        GROUP BY 1, 2 ORDER BY cost_usd DESC, calls DESC
        """,
        (days,),
    )
    totals = db.query_one(
        """
        SELECT
          COALESCE(SUM(cost_usd) FILTER (WHERE created_at >= CURRENT_DATE), 0)      AS today,
          COALESCE(SUM(cost_usd) FILTER (WHERE created_at >= CURRENT_DATE - 7), 0)  AS week,
          COALESCE(SUM(cost_usd) FILTER (WHERE created_at >= CURRENT_DATE - 30), 0) AS month
        FROM llm_usage
        """
    ) or {"today": 0, "week": 0, "month": 0}
    return {
        "daily": [
            {
                **d,
                "date": d["date"].isoformat(),
                "cost_usd": float(d["cost_usd"]) if d["cost_usd"] is not None else None,
            }
            for d in daily
        ],
        "by_model": [
            {
                **m,
                "cost_usd": float(m["cost_usd"]) if m["cost_usd"] is not None else None,
            }
            for m in by_model
        ],
        "totals": {k: float(v or 0) for k, v in totals.items()},
    }
