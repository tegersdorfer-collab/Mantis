"""Tests für die Ebbinghaus-Retention-Mathematik (memory/forgetting.py).
Reine Funktionen, kein DB.
"""

import math
import os
import sys
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.forgetting import compute_retention, should_forget, DELETE_THRESHOLD


def test_fresh_memory_full_retention():
    # 0 Tage → exp(0) = 1.0
    assert compute_retention(0.5, 0, days=0) == 1.0


def test_retention_decays_over_time():
    r1 = compute_retention(0.5, 0, days=1)
    r7 = compute_retention(0.5, 0, days=7)
    assert 0 < r7 < r1 < 1.0


def test_min_stability_one_day_survives():
    # importance 0.5, recall 0 → S = max(0.5, 1.0) = 1.0. Nach 1 Tag: exp(-1) ≈ 0.368
    assert compute_retention(0.5, 0, days=1) == math.exp(-1)


def test_recall_count_increases_stability():
    # Mehr Wiederholungen → höhere Retention bei gleicher Zeit
    low  = compute_retention(1.0, 0, days=10)
    high = compute_retention(1.0, 8, days=10)  # S = 1*(1+2) = 3
    assert high > low


def test_should_forget_after_long_time():
    # S=1, nach 3 Tagen: exp(-3) ≈ 0.0498 < 0.10 → vergessen
    assert should_forget(0.5, 0, days=3) is True


def test_should_not_forget_recent():
    assert should_forget(0.5, 0, days=1) is False  # 0.368 > 0.10


def test_high_importance_survives_longer():
    # importance 1.0, recall 4 → S = 1*(1+1) = 2 ... nach 3 Tagen exp(-1.5)=0.223 > 0.10
    assert should_forget(1.0, 4, days=3) is False


def test_threshold_boundary():
    # Punkt, an dem retention == DELETE_THRESHOLD: days = -S*ln(0.10)
    s = 1.0
    days_at_threshold = -s * math.log(DELETE_THRESHOLD)
    assert should_forget(0.5, 0, days=days_at_threshold + 0.01) is True
    assert should_forget(0.5, 0, days=days_at_threshold - 0.01) is False


def test_chat_pruning_uses_configured_ttl_as_a_query_parameter(monkeypatch):
    from memory import forgetting
    import config

    calls = []
    monkeypatch.setattr(config, "CHAT_TTL_DAYS", 42, raising=False)
    monkeypatch.setattr(
        forgetting._db,
        "query_one",
        lambda sql, params=(): calls.append((sql, params)) or {"n": 0},
    )

    pruned = asyncio.run(forgetting.ForgettingCurve()._prune_chat())

    assert pruned == 0
    assert calls[0][1] == (42,)
    assert "make_interval(days => %s)" in calls[0][0]
