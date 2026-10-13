"""Unit-Tests für core/llm_usage.py (Pricing + record, kein DB nötig)."""
import sys, os
import asyncio
import httpx
from datetime import date
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch

from core.llm_usage import cost_usd, record, _fallback_cost


class TestCostUsd:
    def test_haiku_input_only(self):
        # 1M Input-Tokens Haiku = $1.00
        assert cost_usd("claude-haiku-4-5-20251001", 1_000_000, 0) == 1.00

    def test_haiku_output_only(self):
        assert cost_usd("claude-haiku-4-5", 0, 1_000_000) == 5.00

    def test_sonnet_mixed(self):
        # 1M in ($3) + 1M out ($15) = $18
        assert cost_usd("claude-sonnet-5", 1_000_000, 1_000_000) == 18.00

    def test_opus(self):
        assert cost_usd("claude-opus-4-8", 1_000_000, 0) == 15.00

    def test_case_insensitive(self):
        assert cost_usd("Claude-HAIKU-4-5", 1_000_000, 0) == 1.00

    def test_local_model_is_free(self):
        assert _fallback_cost("ollama", "qwen3.5:9b", 500_000, 500_000) == 0.0

    def test_unknown_model_cost_is_unknown(self):
        assert cost_usd("mystery-model", 1000, 1000) is None

    def test_empty_model(self):
        assert cost_usd("", 1000, 1000) is None

    def test_small_call_rounds_sensibly(self):
        # 2000 in + 500 out auf Haiku: 2000*1 + 500*5 = 4500 / 1M = $0.0045
        assert abs(cost_usd("claude-haiku-4-5", 2000, 500) - 0.0045) < 1e-9

    def test_openrouter_models_have_estimates_as_last_fallback(self):
        assert cost_usd("openai/gpt-6-luna", 1_000_000, 1_000_000) == 0.50
        assert cost_usd("google/gemini-3.1-flash-lite", 1_000_000, 1_000_000) == 0.375

    def test_cloud_model_variant_suffix_is_not_mistaken_for_a_local_model(self):
        assert cost_usd("openai/gpt-6-luna:nitro", 1_000_000, 1_000_000) == 0.50
        assert _fallback_cost("openrouter", "qwen3.5:9b", 500_000, 500_000) is None
        assert _fallback_cost("local", "unknown:variant", 500_000, 500_000) is None


class TestRecord:
    def test_zero_tokens_is_noop(self):
        # Darf keinen DB-Zugriff versuchen (early return)
        with patch("core.db.execute") as mock_exec:
            record("claude", "claude-haiku-4-5", 0, 0)
            mock_exec.assert_not_called()

    def test_record_writes_row_sync(self):
        # Ohne laufenden Event-Loop → synchroner Pfad über db.execute
        with patch("core.db.execute") as mock_exec:
            record("claude", "claude-haiku-4-5", 2000, 500, purpose="chat-agent")
            mock_exec.assert_called_once()
            _, params = mock_exec.call_args[0]
            assert params[0] == "claude"
            assert params[1] == "claude-haiku-4-5"
            assert params[2] == "chat-agent"
            assert params[3] == 2000
            assert params[4] == 500
            assert abs(params[5] - 0.0045) < 1e-9

    def test_record_uses_actual_provider_cost_when_available(self):
        with patch("core.db.execute") as mock_exec:
            record(
                "openrouter", "openai/gpt-6-luna", 2000, 500,
                purpose="chat-agent", actual_cost=0.001237,
            )

            mock_exec.assert_called_once()
            _, params = mock_exec.call_args[0]
            assert params[:5] == ("openrouter", "openai/gpt-6-luna", "chat-agent", 2000, 500)
            assert params[5] == 0.001237

    def test_actual_zero_cost_is_saved_even_without_tokens(self):
        with patch("core.db.execute") as mock_exec:
            record(
                "openrouter", "openai/gpt-6-luna", 0, 0,
                purpose="chat-agent", actual_cost=0.0,
            )

        mock_exec.assert_called_once()
        assert mock_exec.call_args.args[1] == (
            "openrouter", "openai/gpt-6-luna", "chat-agent", 0, 0, 0.0,
        )

    def test_unknown_openrouter_cost_is_stored_as_null(self):
        with patch("core.db.execute") as mock_exec:
            record("openrouter", "vendor/unknown", 12, 5)

        assert mock_exec.call_args.args[1][5] is None

    def test_generation_cost_is_fetched_async_then_backfilled(self, monkeypatch):
        from core import db
        import core.llm_usage as llm_usage

        inserted = []
        updates = []
        update_ready = asyncio.Event()

        async def insert(sql, params):
            inserted.append((sql, params))
            return 41

        async def update(sql, params):
            updates.append((sql, params))
            update_ready.set()
            return 1

        fetched = []

        async def fetch(generation_id, api_key, client=None):
            fetched.append((generation_id, api_key, client))
            return 0.004321

        monkeypatch.setattr(db, "ainsert_returning", insert)
        monkeypatch.setattr(db, "aexecute", update)
        monkeypatch.setattr(llm_usage, "_fetch_openrouter_generation_cost", fetch)

        async def run():
            record(
                "openrouter", "vendor/unknown", 12, 5,
                purpose="chat-agent", generation_id="gen-41", api_key="synthetic-key",
            )
            await asyncio.wait_for(update_ready.wait(), timeout=1)

        asyncio.run(run())

        assert inserted[0][1] == ("openrouter", "vendor/unknown", "chat-agent", 12, 5, None)
        assert fetched == [("gen-41", "synthetic-key", None)]
        assert updates[0][1] == (0.004321, 41)

    def test_generation_lookup_uses_openrouter_endpoint_and_reads_zero_cost(self):
        from core.llm_usage import _fetch_openrouter_generation_cost

        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["authorization"] = request.headers.get("authorization")
            return httpx.Response(200, json={"data": {"total_cost": 0}})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://openrouter.ai/api/v1",
        )
        cost = asyncio.run(_fetch_openrouter_generation_cost("gen-0", "synthetic-key", client))

        assert cost == 0.0
        assert seen["url"].endswith("/generation?id=gen-0")
        assert seen["authorization"] == "Bearer synthetic-key"

    def test_generation_lookup_failure_is_tolerated_and_uses_known_price(self, monkeypatch):
        from core import db
        import core.llm_usage as llm_usage

        inserted = []
        updates = []
        update_ready = asyncio.Event()

        async def insert(sql, params):
            inserted.append(params)
            return 52

        async def update(sql, params):
            updates.append(params)
            update_ready.set()
            return 1

        async def fetch(generation_id, api_key, client=None):
            raise RuntimeError("synthetic lookup failure")

        monkeypatch.setattr(db, "ainsert_returning", insert)
        monkeypatch.setattr(db, "aexecute", update)
        monkeypatch.setattr(llm_usage, "_fetch_openrouter_generation_cost", fetch)

        async def run():
            record(
                "openrouter", "openai/gpt-6-luna", 1_000_000, 1_000_000,
                generation_id="gen-failed", api_key="synthetic-key",
            )
            await asyncio.wait_for(update_ready.wait(), timeout=1)

        asyncio.run(run())

        assert inserted[0][5] is None
        assert updates[0][0] == 0.50

    def test_generation_lookup_has_an_overall_timeout(self, monkeypatch):
        from core import db
        import core.llm_usage as llm_usage

        cancelled = []
        monkeypatch.setattr(llm_usage, "GENERATION_LOOKUP_TIMEOUT_S", 0.02)
        monkeypatch.setattr(db, "ainsert_returning", lambda *_: _async_value(61))
        monkeypatch.setattr(db, "aexecute", lambda *_: _async_value(1))

        async def hanging_fetch(*args, **kwargs):
            try:
                await asyncio.Future()
            finally:
                cancelled.append(True)

        monkeypatch.setattr(llm_usage, "_fetch_openrouter_generation_cost", hanging_fetch)

        async def run():
            await asyncio.wait_for(llm_usage._record_with_generation_cost(
                "openrouter", "vendor/unknown", "chat-agent", 12, 5,
                "gen-slow", "synthetic-key", None,
            ), timeout=0.5)

        asyncio.run(run())

        assert cancelled == [True]

    def test_shutdown_cancels_pending_generation_lookup_tasks(self, monkeypatch):
        from core import db
        import core.llm_usage as llm_usage

        started = asyncio.Event()
        cancelled = []
        monkeypatch.setattr(db, "ainsert_returning", lambda *_: _async_value(62))

        async def hanging_fetch(*args, **kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.append(True)

        monkeypatch.setattr(llm_usage, "_fetch_openrouter_generation_cost", hanging_fetch)

        async def run():
            llm_usage.record(
                "openrouter", "vendor/unknown", 12, 5,
                generation_id="gen-shutdown", api_key="synthetic-key",
            )
            await asyncio.wait_for(started.wait(), timeout=0.5)
            await llm_usage.shutdown_pending_generation_lookups()
            assert not llm_usage._pending_generation_lookups

        asyncio.run(run())

        assert cancelled == [True]

    def test_summary_keeps_unknown_model_cost_null(self, monkeypatch):
        from core import db
        from core.llm_usage import summary

        rows = [
            [{
                "date": date(2026, 10, 13), "input_tokens": 10,
                "output_tokens": 5, "cost_usd": None,
            }],
            [{
                "provider": "openrouter", "model": "vendor/unknown", "calls": 1,
                "input_tokens": 10, "output_tokens": 5, "cost_usd": None,
            }],
        ]

        def query(sql, *_):
            return rows.pop(0)

        monkeypatch.setattr(db, "query", query)
        monkeypatch.setattr(db, "query_one", lambda *_: {"today": 0, "week": 0, "month": 0})

        result = summary()

        assert result["daily"][0]["cost_usd"] is None
        assert result["by_model"][0]["cost_usd"] is None

    def test_api_costs_labels_unknown_cost_as_unknown(self, monkeypatch):
        from core import llm_usage
        from core.skills.system import _api_costs

        monkeypatch.setattr(llm_usage, "summary", lambda *_: {
            "totals": {"today": 0, "week": 0, "month": 0},
            "by_model": [{
                "provider": "openrouter", "model": "vendor/unknown", "calls": 1,
                "input_tokens": 10, "output_tokens": 5, "cost_usd": None,
            }],
        })

        result = asyncio.run(_api_costs())

        assert "unbekannt" in result
        assert "kostenlos (lokal)" not in result

    def test_record_swallows_db_errors(self):
        with patch("core.db.execute", side_effect=RuntimeError("db down")):
            # Darf keine Exception nach außen werfen
            record("ollama", "qwen3.5:9b", 100, 100)

    def test_none_tokens_treated_as_zero(self):
        with patch("core.db.execute") as mock_exec:
            record("ollama", "qwen3.5:9b", None, 300)
            mock_exec.assert_called_once()
            _, params = mock_exec.call_args[0]
            assert params[3] == 0
            assert params[4] == 300


async def _async_value(value):
    return value
