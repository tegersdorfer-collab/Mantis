"""Fachregeln für die Merk-Inbox-Erkennung und Klassifikation."""

import pytest

from domains.inbox.capture import (
    classify_capture,
    classify_item,
    extract_source_urls,
    normalize_url,
    platform_for_url,
)


@pytest.mark.parametrize("text", [
    "https://youtu.be/abc123",
    "Schau ich mir später an https://example.org/post",
    "https://www.tiktok.com/@name/video/42",
    "https://www.instagram.com/reel/abc/",
    "https://example.org/post.utm_source=share",
])
def test_single_link_without_request_is_a_capture(text):
    assert classify_capture(text).capture is True


@pytest.mark.parametrize("text", [
    "Was ist das? https://youtu.be/abc123",
    "Fass das bitte zusammen: https://example.org/article",
    "Erklär mir dieses Video https://youtu.be/abc123",
    "Kannst du den Link prüfen? https://example.org",
    "Übersetze den Artikel https://example.org/article",
    "Such mir ähnliche Produkte zu https://shop.example/item",
    "Öffne diesen Link https://example.org",
    "Ist das seriös? https://example.org",
    "Kannst du mir sagen, worum es geht: https://youtu.be/abc123",
])
def test_link_with_question_or_work_request_stays_in_agent(text):
    assert classify_capture(text).capture is False


@pytest.mark.parametrize("text", [
    "Merk dir das: Balkon mit Kräutern und Sitzbank",
    "Für später https://example.org/guide",
    "Speichere diese Idee: Regal unter dem Fenster",
    "Notier: https://example.org/recipe",
    "Behalte das für mich im Kopf",
    "Pack das auf meine Merkliste",
    "Das will ich später kaufen: https://shop.example/item",
])
def test_explicit_save_instruction_captures_text_or_link(text):
    assert classify_capture(text).capture is True


@pytest.mark.parametrize("text", [
    "Hi, wie war dein Tag?",
    "Ich überlege, im Frühjahr einen Balkon zu bauen.",
    "Kannst du mir mit Python helfen?",
    "Danke, mache ich später.",
    "https://example.org ist kaputt, kannst du das fixen?",
    "Lösch bitte meine letzte Notiz.",
    "Was wollte ich mir noch anschauen?",
    "https://example.org und https://example.net, welchen Link findest du besser?",
    "Merk dir das und fasse den Artikel zusammen: https://example.org/article",
])
def test_regular_chat_or_ambiguous_request_is_not_silently_captured(text):
    assert classify_capture(text).capture is False


def test_image_without_caption_is_a_capture_but_text_without_link_is_not():
    assert classify_capture("", has_image=True).capture is True
    assert classify_capture("Balkon-Idee", has_image=True).capture is True
    assert classify_capture("Was ist das?", has_image=True).capture is False
    assert classify_capture("Eine Idee für nächstes Jahr").capture is False


def test_url_extraction_strips_sentence_punctuation_and_rejects_other_schemes():
    assert extract_source_urls(
        "Hier: https://Example.org/a?b=2). und javascript:alert(1)"
    ) == ["https://Example.org/a?b=2"]


@pytest.mark.parametrize(("url", "platform"), [
    ("https://youtu.be/abc", "youtube"),
    ("https://m.youtube.com/watch?v=abc", "youtube"),
    ("https://vm.tiktok.com/xyz", "tiktok"),
    ("https://www.instagram.com/reel/abc", "instagram"),
    ("https://blog.example.org/post", "article"),
])
def test_platform_is_derived_from_the_url_host(url, platform):
    assert platform_for_url(url) == platform


def test_url_normalization_deduplicates_tracking_and_fragment_variants():
    assert normalize_url("HTTPS://WWW.Example.org:443/path/?utm_source=discord#top") == (
        "https://example.org/path"
    )


@pytest.mark.parametrize(("text", "metadata", "expected"), [
    ("Merk dir als Zitat: 'Der Weg ist das Ziel'", {}, "zitat"),
    ("Balkon mit Hochbeet", {}, "idee"),
    ("Restaurant in Kopenhagen", {}, "ort"),
    ("Dune: Part Two ist ein Film", {}, "film-serie"),
    ("Das Angebot will ich kaufen", {}, "kaufen"),
    ("", {"platform": "youtube", "type": "video"}, "anschauen"),
    ("", {"platform": "article", "type": "article"}, "lesen"),
    ("", {}, "sonstiges"),
])
def test_all_inbox_categories_have_deterministic_rules(text, metadata, expected):
    category, _tags = classify_item(text, metadata)

    assert category == expected


def test_tags_are_normalized_and_source_is_not_a_topic_tag():
    _category, tags = classify_item(
        "Arrival Sci-Fi Film",
        {"platform": "youtube", "title": "Arrival"},
    )

    assert tags == ["scifi"]
