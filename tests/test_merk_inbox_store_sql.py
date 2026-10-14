"""SQL-Konstruktion für Inbox-Suche und eindeutige Textauflösung."""

from domains.inbox.query import SearchQuery
from domains.inbox import store


def test_status_all_search_adds_where_before_keyword_filter(monkeypatch):
    calls = []
    monkeypatch.setattr(store.db, "query", lambda sql, params: calls.append((sql, params)) or [])

    assert store.search_items(SearchQuery(query="Arrival", status=None)) == []

    sql = calls[0][0]
    assert "FROM merk_inbox_items WHERE (title ILIKE" in sql
    assert "FROM merk_inbox_items AND" not in sql


def test_ilike_wildcards_are_escaped_in_search_and_item_resolution(monkeypatch):
    calls = []
    monkeypatch.setattr(store.db, "query", lambda sql, params: calls.append((sql, params)) or [])

    store.search_items(SearchQuery(query=r"10%_\\", status="all"))
    store.delete_item(query="10%")

    search_sql, search_params = calls[0]
    resolve_sql, resolve_params = calls[1]
    assert "ESCAPE '!'" in search_sql
    assert "ESCAPE '!'" in resolve_sql
    assert search_params[:4] == (r"%10!%!_\\%",) * 4
    assert search_params[4:8] == (r"%10!%!_\\%",) * 4
    assert resolve_params == ("%10!%%", "%10!%%")


def test_open_link_merge_registers_the_new_discord_message_id(monkeypatch):
    calls = []
    existing = {
        "id": 17,
        "source_message_id": "message-a",
        "source_message_ids": ["message-a"],
        "source_urls": ["https://example.org/item"],
        "normalized_urls": ["https://example.org/item"],
        "user_note": "",
        "source_kind": "link",
    }

    def query_one(sql, params):
        calls.append((sql, params))
        return {
            **existing,
            "source_message_ids": [*existing["source_message_ids"], params[6]],
        }

    monkeypatch.setattr(store.db, "query_one", query_one)
    updated = store._append_duplicate(
        existing, ["https://example.org/item"], ["https://example.org/item"],
        "", "link", source_message_id="message-b",
    )

    assert "array_append(source_message_ids, %s)" in calls[0][0]
    assert calls[0][1][4:7] == ("message-b",) * 3
    assert updated["source_message_ids"] == ["message-a", "message-b"]


def test_enrichment_title_update_is_guarded_by_the_expected_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(store.db, "query_one", lambda sql, params: calls.append((sql, params)) or {})

    store.update_enrichment(17, title="Arrival", title_if_current="YouTube-Link")

    sql, params = calls[0]
    assert "title=CASE WHEN title=%s THEN %s ELSE title END" in sql
    assert params[:2] == ("YouTube-Link", "Arrival")
