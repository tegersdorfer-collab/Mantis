"""Unit-Tests für core/llm_usage.py (Pricing + record, kein DB nötig)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch

from core.llm_usage import cost_usd, record


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
        assert cost_usd("qwen3.5:9b", 500_000, 500_000) == 0.0

    def test_unknown_model_is_free(self):
        assert cost_usd("mystery-model", 1000, 1000) == 0.0

    def test_empty_model(self):
        assert cost_usd("", 1000, 1000) == 0.0

    def test_small_call_rounds_sensibly(self):
        # 2000 in + 500 out auf Haiku: 2000*1 + 500*5 = 4500 / 1M = $0.0045
        assert abs(cost_usd("claude-haiku-4-5", 2000, 500) - 0.0045) < 1e-9


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
