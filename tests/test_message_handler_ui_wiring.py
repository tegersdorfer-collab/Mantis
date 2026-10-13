"""Testet, dass MessageHandler nach jedem Turn maybe_update_ui aufruft
(reine Verkabelungs-Prüfung, keine echten Collaborators nötig)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from core.message_handler import MessageHandler


def _make_handler():
    agent = MagicMock()
    agent.run = AsyncMock(return_value=(
        "Du hast 7.2h geschlafen.",
        [{"tool": "get_health", "args": {}, "result": "..."}],
    ))
    agent.model_name = "test-model"

    prompt_builder = MagicMock()
    prompt_builder.build = AsyncMock(return_value="system prompt")

    kzg = MagicMock()
    kzg.add = MagicMock()

    lzg = MagicMock()
    lzg.save_kzg_turn = MagicMock()

    alphaprogression = MagicMock()
    alphaprogression.extract_link = MagicMock(return_value=None)

    return MessageHandler(
        kzg=kzg, lzg=lzg, agent=agent, prompt_builder=prompt_builder,
        channel=MagicMock(), proactive_tracker=MagicMock(),
        forgetting=MagicMock(), extractor=MagicMock(), bg_llm=MagicMock(),
        alphaprogression=alphaprogression, on_user_active=MagicMock(),
    )


class TestDashboardRespondUiWiring:
    def test_ruft_maybe_update_ui_mit_genutzten_tools_auf(self):
        handler = _make_handler()
        with patch("core.message_handler.ui_state.maybe_update_ui") as mock_update:
            with patch("core.message_handler.db"):  # DB-Persistenz überspringen
                asyncio.run(handler.dashboard_respond("Wie war mein Schlaf?"))
            mock_update.assert_called_once_with(["get_health"])

    def test_kein_absturz_wenn_maybe_update_ui_fehlschlaegt(self):
        handler = _make_handler()
        with patch("core.message_handler.ui_state.maybe_update_ui",
                   side_effect=RuntimeError("kaputt")):
            with patch("core.message_handler.db"):
                # darf keine Exception nach außen werfen
                response, trace = asyncio.run(handler.dashboard_respond("Wie war mein Schlaf?"))
                assert response == "Du hast 7.2h geschlafen."

    def test_interaktiver_turn_hat_viertausend_token_budget(self, monkeypatch):
        handler = _make_handler()

        async def select_tools(_):
            return [], False

        monkeypatch.setattr("core.message_handler.fast_commands.match", lambda _: None)
        monkeypatch.setattr("core.message_handler.skill_request.match", lambda _: None)
        monkeypatch.setattr("core.message_handler.skills.T.select_tools_async", select_tools)

        asyncio.run(handler._agent_turn("Hallo", channel_name="web"))

        assert handler.agent.run.await_args.kwargs["max_tokens"] == 4000
