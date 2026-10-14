"""Natürliche Inbox-Abfragen werden in enge, nachvollziehbare Filter übersetzt."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from domains.inbox.query import is_inbox_request, parse_natural_query


BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=BERLIN)


def test_what_i_still_wanted_to_watch_filters_open_watch_items():
    query = parse_natural_query("Was wollte ich mir noch anschauen?", now=NOW)

    assert query.query == ""
    assert query.category == "anschauen"
    assert query.status == "open"
    assert query.has_url is None


def test_film_question_filters_open_film_and_series_items():
    query = parse_natural_query("Welche Filme habe ich mir gemerkt?", now=NOW)

    assert query.category == "film-serie"
    assert query.status == "open"


def test_balkon_idea_keeps_the_topic_as_the_semantic_query():
    query = parse_natural_query("Was war die Idee mit dem Balkon?", now=NOW)

    assert query.query == "Balkon"
    assert query.category == "idee"
    assert query.status == "open"


def test_links_from_last_week_use_previous_berlin_calendar_week():
    query = parse_natural_query("Zeig mir Links von letzter Woche", now=NOW)

    assert query.has_url is True
    assert query.created_after == datetime(2026, 10, 5, 0, 0, tzinfo=BERLIN)
    assert query.created_before == datetime(2026, 10, 12, 0, 0, tzinfo=BERLIN)
    assert query.status == "open"


@pytest.mark.parametrize("text", [
    "Was habe ich schon gesehen?",
    "Zeig mir erledigte Merker",
])
def test_done_questions_select_completed_items(text):
    assert parse_natural_query(text, now=NOW).status == "done"


def test_query_for_everything_does_not_apply_open_status_by_default():
    query = parse_natural_query("Zeig mir alle Merker", now=NOW)

    assert query.status is None


def test_platform_name_selects_its_linked_watch_items():
    query = parse_natural_query("Zeig mir YouTube-Merker", now=NOW)

    assert query.has_url is True
    assert query.category == "anschauen"


@pytest.mark.parametrize("text", [
    "Hi, wie geht es dir?",
    "Was wollte ich mir noch anschauen?",
    "Lösch bitte den Merker über Arrival",
    "Korrigiere den Titel des Merkers Arrival auf Dune",
])
def test_query_parser_never_turns_general_or_mutating_requests_into_search_text(text):
    result = parse_natural_query(text, now=NOW)

    assert result.is_mutation is (text.startswith(("Lösch", "Korrigiere")))


@pytest.mark.parametrize("text", [
    "Was wollte ich mir noch anschauen?",
    "Welche Filme habe ich mir gemerkt?",
    "Was war die Idee mit dem Balkon?",
    "Zeig mir Links von letzter Woche",
    "Hab ich gesehen",
    "Lösch den Merker Arrival aus meiner Merkliste",
    "Korrigiere den Titel des Merkers Arrival auf Dune",
])
def test_specific_recall_and_management_phrases_route_to_inbox(text):
    assert is_inbox_request(text) is True


@pytest.mark.parametrize("text", [
    "Was ist Arrival?",
    "Welche Filme laufen heute im Kino?",
    "Kannst du das zusammenfassen: https://example.org/article",
    "Ich habe mir gestern einen Film angesehen.",
    "Hi, wie geht es dir?",
])
def test_general_chat_and_link_work_requests_do_not_route_to_inbox(text):
    assert is_inbox_request(text) is False


def test_reply_reference_routes_short_management_command_to_inbox():
    assert is_inbox_request("Lösch das bitte", has_reference=True) is True


@pytest.mark.parametrize("text", [
    "Lösch Merker ID 42",
    "Lösch #42",
    "Korrigiere Eintrag 43",
])
def test_mutation_with_current_message_item_id_routes_to_inbox(text):
    assert is_inbox_request(text) is True


def test_inbox_agent_tools_are_available_without_exposing_second_brain_tools():
    from core import skills  # noqa: F401
    from core.tools import select_tools

    selected = select_tools("Welche Filme habe ich mir gemerkt?")

    assert "inbox_search" in selected
    assert set(selected) == {"inbox_search", "inbox_complete", "inbox_correct", "inbox_delete"}


def test_inbox_search_phrase_forces_an_inbox_tool_call():
    from core import skills  # noqa: F401
    from core.tools import select_tools_async

    selected, force = __import__("asyncio").run(
        select_tools_async("Was wollte ich mir noch anschauen?")
    )

    assert "inbox_search" in selected
    assert set(selected) == {"inbox_search", "inbox_complete", "inbox_correct", "inbox_delete"}
    assert force is True
