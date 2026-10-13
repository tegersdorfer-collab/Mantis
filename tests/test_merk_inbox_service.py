"""Fast-Path-Erfassung: DB-Commit und Bestätigung kommen vor jeder Anreicherung."""

import asyncio
import gc
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from domains.inbox import service


def _item(**values):
    item = {
        "id": 41,
        "title": "YouTube-Link",
        "category": "anschauen",
        "tags": [],
        "status": "open",
        "created_at": datetime(2026, 10, 12, 10, 0),
        "user_note": "",
        "source_urls": ["https://youtu.be/abc"],
    }
    item.update(values)
    return item


def test_link_is_committed_and_confirmed_before_metadata_finishes(monkeypatch):
    async def run():
        release = asyncio.Event()
        metadata_started = asyncio.Event()
        saved = []
        sent = []

        async def slow_metadata(_url, **_kwargs):
            metadata_started.set()
            await release.wait()
            return {"type": "video", "title": "Arrival", "platform": "youtube"}

        monkeypatch.setattr(service.store, "create_item", lambda **kw: (
            saved.append(kw) or {"item": _item(), "created": True, "duplicate_link": False}
        ))
        monkeypatch.setattr(service.metadata, "fetch_metadata", slow_metadata)
        monkeypatch.setattr(service.store, "update_enrichment", lambda *_a, **_k: None)
        monkeypatch.setattr(service.db, "log_event", lambda *_a, **_k: None)
        channel = SimpleNamespace(send=lambda text: _append(sent, text))
        msg = SimpleNamespace(
            text="https://youtu.be/abc", source_message_id="discord-1",
            attachments=(),
        )

        result = await service.capture_discord_message(
            msg, channel, vision_backend=object(), embedding_provider=None,
        )
        await metadata_started.wait()

        assert result["status"] == "created"
        assert sent[0].startswith("📥 gemerkt")
        assert "YouTube-Link" in sent[0]
        assert saved[0]["source_message_id"] == "discord-1"
        assert not result["enrichment_task"].done()

        release.set()
        await result["enrichment_task"]

    asyncio.run(run())


def test_no_cloud_vision_keeps_caption_and_mentions_the_limit(monkeypatch):
    sent = []
    saved = []
    monkeypatch.setattr(service.store, "create_item", lambda **kw: (
        saved.append(kw) or {"item": _item(title="Balkon"), "created": True, "duplicate_link": False}
    ))
    monkeypatch.setattr(service.db, "log_event", lambda *_a, **_k: None)
    msg = SimpleNamespace(
        text="Balkon-Idee", source_message_id="discord-image-1",
        attachments=({"content_type": "image/png", "data": b"image bytes"},),
            attachment_notice="Ein PDF wurde ignoriert.",
    )
    channel = SimpleNamespace(send=lambda text: _append(sent, text))

    result = asyncio.run(service.capture_discord_message(
        msg, channel, vision_backend=object(), embedding_provider=None,
    ))

    assert result["status"] == "created"
    assert saved[0]["image_description"] == "Balkon-Idee"
    assert saved[0]["source_kind"] == "mixed"
    assert "Bildanalyse nicht verfügbar" in sent[0]
    assert "Ein PDF wurde ignoriert." in sent[0]
    assert b"image bytes" not in repr(saved).encode()
    assert result["enrichment_task"] is None


def test_vision_error_does_not_cancel_capture_or_persist_image_bytes(monkeypatch):
    async def run():
        sent = []
        saved = []
        updates = []
        backend = SimpleNamespace(supports_vision=True)
        monkeypatch.setattr(service.store, "create_item", lambda **kw: (
            saved.append(kw) or {"item": _item(title="Bild aus Discord"), "created": True, "duplicate_link": False}
        ))
        monkeypatch.setattr(service.store, "update_enrichment", lambda *a, **kw: updates.append((a, kw)))
        monkeypatch.setattr(service.vision, "describe_images", AsyncMock(side_effect=RuntimeError("Vision down")))
        monkeypatch.setattr(service.db, "log_event", lambda *_a, **_k: None)
        msg = SimpleNamespace(
            text="", source_message_id="discord-image-2",
            attachments=({"content_type": "image/webp", "data": b"private image bytes"},),
        )
        channel = SimpleNamespace(send=lambda text: _append(sent, text))

        result = await service.capture_discord_message(
            msg, channel, vision_backend=backend, embedding_provider=None,
        )
        await result["enrichment_task"]

        assert result["status"] == "created"
        assert sent and sent[0].startswith("📥 gemerkt")
        assert "private image bytes" not in repr(saved)
        assert updates[0][1]["image_description"] == "Bild aus Discord"

    asyncio.run(run())


def test_existing_open_link_uses_duplicate_confirmation(monkeypatch):
    sent = []
    duplicate = _item(created_at=datetime(2026, 10, 12, 10, 0))
    monkeypatch.setattr(service.store, "create_item", lambda **_kw: {
        "item": duplicate, "created": False, "duplicate_link": True,
    })
    monkeypatch.setattr(service.db, "log_event", lambda *_a, **_k: None)
    monkeypatch.setattr(service.metadata, "fetch_metadata", AsyncMock(return_value={}))
    monkeypatch.setattr(service.store, "update_enrichment", lambda *_a, **_k: None)
    msg = SimpleNamespace(text="https://youtu.be/abc", source_message_id="discord-2", attachments=())
    channel = SimpleNamespace(send=lambda text: _append(sent, text))

    result = asyncio.run(service.capture_discord_message(
        msg, channel, vision_backend=object(), embedding_provider=None,
    ))

    assert result["status"] == "duplicate"
    assert sent[0] == "📌 Hast du dir schon am 12.10. gemerkt · ID 41"


def test_background_enrichment_remains_alive_after_handler_returns(monkeypatch):
    async def run():
        release = asyncio.Event()
        metadata_started = asyncio.Event()

        async def slow_metadata(_url, **_kwargs):
            metadata_started.set()
            await release.wait()
            return {"type": "video", "title": "Arrival", "platform": "youtube"}

        monkeypatch.setattr(service.store, "create_item", lambda **_kw: {
            "item": _item(), "created": True, "duplicate_link": False,
        })
        monkeypatch.setattr(service.metadata, "fetch_metadata", slow_metadata)
        monkeypatch.setattr(service.store, "update_enrichment", lambda *_a, **_k: None)
        monkeypatch.setattr(service.db, "log_event", lambda *_a, **_k: None)
        message = SimpleNamespace(
            text="https://youtu.be/abc", source_message_id="discord-3", attachments=(),
        )
        channel = SimpleNamespace(send=lambda _text: _append([], _text))

        result = await service.capture_discord_message(
            message, channel, vision_backend=object(), embedding_provider=None,
        )
        task = result["enrichment_task"]
        await metadata_started.wait()
        del result
        gc.collect()

        assert task in service._BACKGROUND_TASKS
        release.set()
        await task
        await asyncio.sleep(0)
        assert task not in service._BACKGROUND_TASKS

    asyncio.run(run())


def test_capture_uses_existing_local_embedding_provider_after_commit(monkeypatch):
    async def run():
        saved = []
        updates = []

        class Embedder:
            async def embed(self, text):
                saved.append(text)
                return [0.5] * 1024

        monkeypatch.setattr(service.store, "create_item", lambda **kw: {
            "item": _item(title="Balkon"), "created": True, "duplicate_link": False,
        })
        monkeypatch.setattr(service.store, "update_enrichment", lambda *a, **kw: updates.append((a, kw)))
        monkeypatch.setattr(service.db, "log_event", lambda *_a, **_k: None)
        message = SimpleNamespace(
            text="Merk dir das: Balkon", source_message_id="discord-embed-1", attachments=(),
        )
        channel = SimpleNamespace(send=lambda text: _append([], text))

        result = await service.capture_discord_message(
            message, channel, vision_backend=object(), embedding_provider=Embedder(),
        )
        await result["enrichment_task"]

        assert saved == ["Balkon\nBalkon"]
        assert len(updates[0][1]["embedding"]) == 1024
        assert updates[0][1]["embedding"] == [0.5] * 1024

    asyncio.run(run())


async def _append(values, value):
    values.append(value)
