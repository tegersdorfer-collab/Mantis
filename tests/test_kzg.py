"""Tests für das Kurzzeitgedächtnis (memory/kzg.py) — reine In-Memory-Logik:
Checkpoint-Markierung, Hard-Fallback-Trimmen, Token-budgetierte recent_messages,
Summary-Injektion. Kein LLM/DB.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.kzg import KZG


def test_add_and_len():
    k = KZG(max_turns=10)
    k.add("user", "hallo")
    k.add("assistant", "hi")
    assert len(k) == 2
    assert not k.is_empty()


def test_empty_and_whitespace_turns_are_not_added_to_kzg():
    k = KZG(max_turns=10)

    k.add("user", "")
    k.add("assistant", "  \n\t")

    assert k.is_empty()


def test_checkpoint_marked_when_exceeding_max():
    k = KZG(max_turns=4)
    for i in range(4):
        k.add("user", f"m{i}")
    assert k.should_checkpoint() is False
    k.add("user", "eins zu viel")
    assert k.should_checkpoint() is True


def test_hard_fallback_trims_to_max():
    k = KZG(max_turns=4)  # Hard-Limit = 6
    for i in range(7):
        k.add("user", f"m{i}")
    assert len(k) == 4              # auf die letzten max_turns gekürzt
    assert k.get_turns()[0].content == "m3"  # älteste drei verworfen


def test_apply_checkpoint_removes_compressed_and_resets_flag():
    k = KZG(max_turns=4)
    for i in range(5):
        k.add("user", f"m{i}")
    assert k.should_checkpoint()
    k.apply_checkpoint("Zusammenfassung", compress_count=2)
    assert k.should_checkpoint() is False
    assert len(k) == 3                       # 5 - 2 komprimierte
    assert k.get_turns()[0].content == "m2"


def test_recent_messages_prepends_checkpoint_summary():
    k = KZG(max_turns=10)
    k.add("user", "frage")
    k.apply_checkpoint("frühere Themen", compress_count=0)
    msgs = k.recent_messages()
    assert msgs[0].role == "user"
    assert "frühere Themen" in msgs[0].content


def test_recent_messages_respects_min_turns():
    k = KZG(max_turns=50)
    for i in range(10):
        k.add("user", "x" * 4000)  # jeder Turn ~1000 Tokens, Budget 3000
    msgs = k.recent_messages(max_tokens=3000, min_turns=4)
    # trotz Budget-Überschreitung mindestens min_turns
    assert len(msgs) >= 4


def test_get_turns_for_summary_oldest_half_min_four():
    k = KZG(max_turns=50)
    for i in range(10):
        k.add("user", f"m{i}")
    half = k.get_turns_for_summary()
    assert len(half) == 5 and half[0].content == "m0"

    k2 = KZG(max_turns=50)
    for i in range(3):
        k2.add("user", f"m{i}")
    assert len(k2.get_turns_for_summary()) >= min(4, 3) or len(k2.get_turns_for_summary()) == 3


def test_last_user_message():
    k = KZG()
    k.add("user", "erste")
    k.add("assistant", "antwort")
    k.add("user", "zweite")
    assert k.last_user_message() == "zweite"


def test_token_estimate():
    k = KZG()
    k.add("user", "x" * 40)  # 40 Zeichen ≈ 10 Tokens
    assert k.token_estimate() == 10


def test_clear():
    k = KZG()
    k.add("user", "x")
    k.clear()
    assert k.is_empty() and len(k) == 0
