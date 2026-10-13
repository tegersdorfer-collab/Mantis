"""
LLM-Usage-Tracking — erfasst Token-Verbrauch und API-Kosten aller LLM-Calls.

record() ist fire-and-forget: Fehler werden geschluckt, das Tracking darf
niemals einen Chat-Turn brechen. Ollama-Calls werden mit Kosten 0 erfasst
(interessant für Auslastung, kostenlos im Betrieb).
"""
import asyncio
import logging

log = logging.getLogger(__name__)

# USD pro 1M Tokens (input, output) — Substring-Match auf den Modellnamen.
# Reihenfolge zählt: spezifischere Muster zuerst.
PRICES: list[tuple[str, float, float]] = [
    ("haiku",  1.00,  5.00),
    ("sonnet", 3.00, 15.00),
    ("opus",  15.00, 75.00),
]


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Kosten in USD für einen Call. Unbekannte/lokale Modelle → 0."""
    m = (model or "").lower()
    for pattern, in_price, out_price in PRICES:
        if pattern in m:
            return (input_tokens * in_price + output_tokens * out_price) / 1_000_000
    return 0.0


def record(provider: str, model: str, input_tokens: int, output_tokens: int,
           purpose: str = "chat", actual_cost: float | None = None) -> None:
    """Schreibt einen Usage-Eintrag. Läuft im Event-Loop asynchron (nicht blockierend),
    außerhalb davon synchron. Fehler werden nur geloggt."""
    if not input_tokens and not output_tokens:
        return
    try:
        from core import db
        cost = (actual_cost if actual_cost is not None
                else cost_usd(model, input_tokens or 0, output_tokens or 0))
        sql = ("INSERT INTO llm_usage (provider, model, purpose, input_tokens, "
               "output_tokens, cost_usd) VALUES (%s,%s,%s,%s,%s,%s)")
        params = (provider, model, purpose, input_tokens or 0, output_tokens or 0, cost)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            task = loop.create_task(db.aexecute(sql, params))
            task.add_done_callback(_log_task_error)
        else:
            db.execute(sql, params)
    except Exception as e:
        log.debug(f"llm_usage.record fehlgeschlagen: {e}")


def _log_task_error(task: asyncio.Task) -> None:
    exc = task.exception() if not task.cancelled() else None
    if exc:
        log.debug(f"llm_usage insert fehlgeschlagen: {exc}")


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
            {**d, "date": d["date"].isoformat(), "cost_usd": float(d["cost_usd"] or 0)}
            for d in daily
        ],
        "by_model": [
            {**m, "cost_usd": float(m["cost_usd"] or 0)} for m in by_model
        ],
        "totals": {k: float(v or 0) for k, v in totals.items()},
    }
