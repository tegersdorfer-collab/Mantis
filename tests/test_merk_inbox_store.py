"""PostgreSQL-Vertrag der Merk-Inbox; läuft ausschließlich gegen mantis_test."""

import psycopg2
import pytest

from core import db
from domains.inbox.query import SearchQuery
from domains.inbox.store import (
    correct_item,
    create_item,
    delete_item,
    mark_done,
    resolve_item,
    search_items,
)


pytestmark = pytest.mark.db
_TEST_PREFIX = "pytest-merk-inbox-"


@pytest.fixture
def inbox_test_db():
    try:
        db.query_one("SELECT 1")
    except psycopg2.OperationalError:
        pytest.skip("mantis_test ist in dieser Sandbox nicht erreichbar")
    db.run_migrations()
    yield
    db.execute(
        "DELETE FROM merk_inbox_items WHERE source_message_id LIKE %s",
        (_TEST_PREFIX + "%",),
    )


def _new(**overrides):
    item = {
        "title": "Arrival",
        "user_note": "Später ansehen",
        "source_kind": "link",
        "source_urls": ["https://example.org/arrival"],
        "platform": "article",
        "source_metadata": {"title": "Arrival"},
        "image_description": "",
        "category": "film-serie",
        "tags": ["scifi"],
        "source_message_id": _TEST_PREFIX + "one",
    }
    item.update(overrides)
    return item


def test_idempotent_migration_can_run_twice(inbox_test_db):
    db.run_migrations()
    db.run_migrations()

    assert db.query_one(
        "SELECT to_regclass('merk_inbox_items') AS table_name"
    )["table_name"] == "merk_inbox_items"


def test_discord_retry_with_same_message_id_returns_one_item(inbox_test_db):
    first = create_item(**_new())
    retry = create_item(**_new(title="Ignored retry title", user_note="should not append"))

    assert first["created"] is True
    assert retry["created"] is False
    assert retry["item"]["id"] == first["item"]["id"]
    assert retry["item"]["user_note"] == "Später ansehen"


def test_same_normalized_open_link_updates_existing_note_and_urls(inbox_test_db):
    first = create_item(**_new())
    duplicate = create_item(**_new(
        source_message_id=_TEST_PREFIX + "two",
        source_urls=["https://www.example.org/arrival/?utm_source=discord#film"],
        user_note="Mit Denis Villeneuve",
    ))

    assert duplicate["created"] is False
    assert duplicate["duplicate_link"] is True
    assert duplicate["item"]["id"] == first["item"]["id"]
    assert duplicate["item"]["user_note"] == "Später ansehen\nMit Denis Villeneuve"
    assert duplicate["item"]["source_urls"] == [
        "https://example.org/arrival",
        "https://www.example.org/arrival/?utm_source=discord#film",
    ]


def test_duplicate_message_id_remains_idempotent_after_item_is_done(inbox_test_db):
    first = create_item(**_new(source_message_id=_TEST_PREFIX + "first"))
    duplicate = create_item(**_new(
        source_message_id=_TEST_PREFIX + "second",
        user_note="Mit Denis Villeneuve",
    ))

    assert duplicate["duplicate_link"] is True
    assert _TEST_PREFIX + "second" in duplicate["item"]["source_message_ids"]

    mark_done(item_id=first["item"]["id"])
    retry = create_item(**_new(
        source_message_id=_TEST_PREFIX + "second",
        user_note="Dieser Retry darf nicht erneut anhängen",
    ))

    assert retry["created"] is False
    assert retry["duplicate_link"] is False
    assert retry["item"]["id"] == first["item"]["id"]
    assert retry["item"]["user_note"] == "Später ansehen\nMit Denis Villeneuve"


def test_done_entry_does_not_block_a_new_item_with_the_same_link(inbox_test_db):
    first = create_item(**_new())
    mark_done(item_id=first["item"]["id"])
    next_item = create_item(**_new(source_message_id=_TEST_PREFIX + "done-again"))

    assert next_item["created"] is True
    assert next_item["item"]["id"] != first["item"]["id"]


def test_search_respects_category_status_url_and_keyword_filters(inbox_test_db):
    open_item = create_item(**_new(source_message_id=_TEST_PREFIX + "open"))["item"]
    done_item = create_item(**_new(
        source_message_id=_TEST_PREFIX + "done",
        title="Dune",
        source_urls=[],
        source_kind="text",
    ))["item"]
    mark_done(item_id=done_item["id"])

    found = search_items(SearchQuery(
        query="Arrival", category="film-serie", status="open", has_url=True,
    ))

    assert [item["id"] for item in found] == [open_item["id"]]


def test_completion_sets_done_and_completed_timestamp(inbox_test_db):
    item = create_item(**_new())["item"]

    result = mark_done(item_id=item["id"])

    assert result["status"] == "done"
    assert result["item"]["status"] == "done"
    assert result["item"]["completed_at"] is not None


def test_completion_can_resolve_reply_to_source_and_recent_single_item(inbox_test_db):
    item = create_item(**_new())["item"]

    by_reply = mark_done(reference=_TEST_PREFIX + "one")
    assert by_reply["item"]["id"] == item["id"]

    create_item(**_new(source_message_id=_TEST_PREFIX + "two", title="Dune",
                       source_urls=["https://example.com/dune"]))
    create_item(**_new(source_message_id=_TEST_PREFIX + "three", title="Blade Runner",
                       source_urls=["https://example.com/blade-runner"]))
    ambiguous = mark_done(allow_recent=True)
    assert ambiguous["status"] == "ambiguous"


def test_correction_changes_only_named_fields(inbox_test_db):
    item = create_item(**_new())["item"]

    updated = correct_item(item["id"], title="Arrival (2016)")

    assert updated["title"] == "Arrival (2016)"
    assert updated["category"] == "film-serie"
    assert updated["tags"] == ["scifi"]
    assert updated["source_urls"] == ["https://example.org/arrival"]


def test_delete_requires_unique_match_and_removes_the_row(inbox_test_db):
    first = create_item(**_new(source_message_id=_TEST_PREFIX + "arrival-1"))["item"]
    create_item(**_new(source_message_id=_TEST_PREFIX + "arrival-2",
                       source_urls=["https://example.com/arrival-2"]))

    ambiguous = delete_item(query="Arrival")
    assert ambiguous["status"] == "ambiguous"
    assert db.query_one("SELECT id FROM merk_inbox_items WHERE id=%s", (first["id"],))

    deleted = delete_item(item_id=first["id"])
    assert deleted["status"] == "deleted"
    assert db.query_one("SELECT id FROM merk_inbox_items WHERE id=%s", (first["id"],)) is None


def test_search_resolution_and_delete_treat_like_wildcards_as_literal(inbox_test_db):
    item = create_item(**_new(
        title="1000 Tipps",
        source_kind="text",
        source_urls=[],
        source_message_id=_TEST_PREFIX + "literal-percent",
    ))["item"]

    matches = search_items(SearchQuery(query="10%", status="all"))
    resolved = resolve_item(query="10%")
    deleted = delete_item(query="10%")

    assert item["id"] not in [row["id"] for row in matches]
    assert resolved["status"] == "not_found"
    assert deleted["status"] == "not_found"
    assert db.query_one("SELECT id FROM merk_inbox_items WHERE id=%s", (item["id"],))
