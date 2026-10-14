"""Testet, dass MessageHandler nach jedem Turn maybe_update_ui aufruft
(reine Verkabelungs-Prüfung, keine echten Collaborators nötig)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
from types import SimpleNamespace
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

    def test_toolauswahl_fehler_nutzt_konservative_keyword_auswahl(self, monkeypatch):
        handler = _make_handler()
        handler.agent.backend = SimpleNamespace(sends_full_tool_catalog=True)

        async def selection_fails(_text):
            raise RuntimeError("Klassifikation ausgefallen")

        monkeypatch.setattr("core.message_handler.fast_commands.match", lambda _: None)
        monkeypatch.setattr("core.message_handler.skill_request.match", lambda _: None)
        monkeypatch.setattr("core.message_handler.skills.T.select_tools_async", selection_fails)
        monkeypatch.setattr(
            "core.message_handler.skills.T.select_tools", lambda _text: ["calculate"],
        )
        monkeypatch.setattr("core.message_handler.skills.T.is_action", lambda _text: False)

        async def run_agent(**kwargs):
            assert await kwargs["tool_selection_task"] == (["calculate"], False)
            return "Antwort", []

        handler.agent.run = run_agent
        async def run():
            await handler._agent_turn("Hallo", channel_name="voice", agent=handler.agent)

        asyncio.run(run())

    def test_backend_mit_gefilterten_schemas_wartet_vor_agent_start(self, monkeypatch):
        handler = _make_handler()
        handler.agent.backend = SimpleNamespace(sends_full_tool_catalog=False)
        order = []

        async def select(_text):
            order.append("selection")
            return ["calculate"], False

        async def run(**_kwargs):
            order.append("agent")
            return "Antwort", []

        handler.agent.run = run
        monkeypatch.setattr("core.message_handler.fast_commands.match", lambda _: None)
        monkeypatch.setattr("core.message_handler.skill_request.match", lambda _: None)
        monkeypatch.setattr("core.message_handler.skills.T.select_tools_async", select)

        asyncio.run(handler._agent_turn("Hallo", channel_name="voice", agent=handler.agent))

        assert order == ["selection", "agent"]

    def test_toolauswahl_wird_nach_direkter_antwort_abgebrochen_und_abgewartet(self, monkeypatch):
        handler = _make_handler()
        handler.agent.backend = SimpleNamespace(sends_full_tool_catalog=True)
        auswahl_gestartet = asyncio.Event()
        bereinigt = asyncio.Event()

        async def select(_text):
            auswahl_gestartet.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                bereinigt.set()

        async def build(_text):
            await auswahl_gestartet.wait()
            return "system prompt"

        handler.prompt_builder.build = build
        monkeypatch.setattr("core.message_handler.fast_commands.match", lambda _: None)
        monkeypatch.setattr("core.message_handler.skill_request.match", lambda _: None)
        monkeypatch.setattr("core.message_handler.skills.T.select_tools_async", select)

        async def run():
            try:
                await handler._agent_turn("Hallo", channel_name="voice", agent=handler.agent)
                await asyncio.sleep(0)
                task = handler.agent.run.await_args.kwargs["tool_selection_task"]
                return task.done(), task.cancelled(), bereinigt.is_set()
            finally:
                task = handler.agent.run.await_args.kwargs.get("tool_selection_task")
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        assert asyncio.run(run()) == (True, True, True)

    def test_toolauswahl_wird_bei_abbruch_abgebrochen_und_abgewartet(self, monkeypatch):
        handler = _make_handler()
        handler.agent.backend = SimpleNamespace(sends_full_tool_catalog=True)
        auswahl_gestartet = asyncio.Event()
        agent_gestartet = asyncio.Event()
        bereinigt = asyncio.Event()
        erfasste_tasks = []

        async def select(_text):
            auswahl_gestartet.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                bereinigt.set()

        async def block_run(**kwargs):
            erfasste_tasks.append(kwargs["tool_selection_task"])
            agent_gestartet.set()
            await asyncio.Event().wait()

        handler.agent.run = block_run
        monkeypatch.setattr("core.message_handler.fast_commands.match", lambda _: None)
        monkeypatch.setattr("core.message_handler.skill_request.match", lambda _: None)
        monkeypatch.setattr("core.message_handler.skills.T.select_tools_async", select)

        async def run():
            parent = asyncio.create_task(
                handler._agent_turn("Hallo", channel_name="voice", agent=handler.agent),
            )
            try:
                await auswahl_gestartet.wait()
                await agent_gestartet.wait()
                parent.cancel()
                try:
                    await parent
                except asyncio.CancelledError:
                    pass
                return (
                    parent.cancelled(),
                    erfasste_tasks[0].done(),
                    erfasste_tasks[0].cancelled(),
                    bereinigt.is_set(),
                )
            finally:
                if not parent.done():
                    parent.cancel()
                    await asyncio.gather(parent, return_exceptions=True)
                for task in erfasste_tasks:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

        assert asyncio.run(run()) == (True, True, True, True)

    def test_toolauswahl_wird_bei_fehler_in_recent_messages_abgebrochen_und_abgewartet(self, monkeypatch):
        handler = _make_handler()
        handler.agent.backend = SimpleNamespace(sends_full_tool_catalog=True)
        handler.kzg.recent_messages.side_effect = RuntimeError("Historie nicht verfügbar")
        auswahl_gestartet = asyncio.Event()
        bereinigt = asyncio.Event()
        auswahl_tasks = []

        async def select(_text):
            auswahl_gestartet.set()
            auswahl_tasks.append(asyncio.current_task())
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                bereinigt.set()

        async def build(_text):
            await auswahl_gestartet.wait()
            return "system prompt"

        handler.prompt_builder.build = build
        monkeypatch.setattr("core.message_handler.fast_commands.match", lambda _: None)
        monkeypatch.setattr("core.message_handler.skill_request.match", lambda _: None)
        monkeypatch.setattr("core.message_handler.skills.T.select_tools_async", select)

        async def run():
            try:
                await handler._agent_turn("Hallo", channel_name="voice", agent=handler.agent)
            except RuntimeError as exc:
                assert str(exc) == "Historie nicht verfügbar"
            task = auswahl_tasks[0]
            status = task.done(), task.cancelled(), bereinigt.is_set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            return status

        result = asyncio.run(run())
        assert result == (True, True, True)
