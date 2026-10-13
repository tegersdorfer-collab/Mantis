from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import config
from web import routers
from web.api import create_app
from web.routers import system


TOKEN = "usage-test-token"
BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc


def test_daily_usage_window_uses_owner_local_dates_for_utc_rows():
    start, end = system._daily_usage_window(
        1, datetime(2026, 10, 13, 12, tzinfo=BERLIN), "Europe/Berlin"
    )

    assert start == datetime(2026, 10, 12, 22, tzinfo=UTC)
    assert end == datetime(2026, 10, 13, 22, tzinfo=UTC)


def test_daily_usage_aggregates_synthetic_messages_by_owner_local_day():
    start, end = system._daily_usage_window(
        1, datetime(2026, 10, 13, 12, tzinfo=BERLIN), "Europe/Berlin"
    )
    rows = [
        {"role": "user", "channel": "web", "created_at": datetime(2026, 10, 12, 22, 30, tzinfo=UTC)},
        {"role": "user", "channel": "web", "created_at": datetime(2026, 10, 12, 22, 45, tzinfo=UTC)},
        {"role": "user", "channel": "telegram", "created_at": datetime(2026, 10, 12, 22, 50, tzinfo=UTC)},
        {"role": "assistant", "channel": "web", "created_at": datetime(2026, 10, 12, 22, 55, tzinfo=UTC)},
        {"role": "user", "channel": "web", "created_at": datetime(2026, 10, 12, 21, 59, tzinfo=UTC)},
        {"role": "user", "channel": "ios", "created_at": datetime(2026, 10, 13, 22, 0, tzinfo=UTC)},
    ]

    assert system._aggregate_daily_usage(rows, start, end, "Europe/Berlin") == [
        {"date": "2026-10-13", "channel": "telegram", "user_messages": 1},
        {"date": "2026-10-13", "channel": "web", "user_messages": 2},
    ]


@pytest.mark.parametrize(
    ("now", "expected_start", "expected_end", "window_hours"),
    [
        (
            datetime(2026, 3, 30, 12, tzinfo=BERLIN),
            datetime(2026, 3, 27, 23, tzinfo=UTC),
            datetime(2026, 3, 30, 22, tzinfo=UTC),
            71,
        ),
        (
            datetime(2026, 10, 26, 12, tzinfo=BERLIN),
            datetime(2026, 10, 23, 22, tzinfo=UTC),
            datetime(2026, 10, 26, 23, tzinfo=UTC),
            73,
        ),
    ],
)
def test_daily_usage_window_respects_daylight_saving_transitions(
    now, expected_start, expected_end, window_hours
):
    start, end = system._daily_usage_window(3, now, "Europe/Berlin")

    assert start == expected_start
    assert end == expected_end
    assert (end - start) == timedelta(hours=window_hours)


def test_daily_usage_requires_auth_and_aggregates_raw_database_rows(monkeypatch):
    calls = []
    monkeypatch.setattr(routers, "ROUTER_MODULES", [system])
    monkeypatch.setattr(config, "DASHBOARD_TOKEN", TOKEN, raising=False)
    monkeypatch.setattr(config, "DASHBOARD_ALLOWED_ORIGINS", "http://localhost:1420", raising=False)
    monkeypatch.setattr(config, "OWNER_TIMEZONE", "Europe/Berlin", raising=False)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime(2026, 10, 13, 12, tzinfo=UTC)
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(system, "datetime", FrozenDatetime)

    rows = [
        {"role": "user", "channel": "web", "created_at": datetime(2026, 10, 12, 22, 30, tzinfo=UTC)},
        {"role": "user", "channel": "web", "created_at": datetime(2026, 10, 12, 22, 45, tzinfo=UTC)},
        {"role": "assistant", "channel": "web", "created_at": datetime(2026, 10, 12, 22, 55, tzinfo=UTC)},
    ]

    def query(sql, params=()):
        calls.append((sql, params))
        return rows

    monkeypatch.setattr(system.db, "query", query)

    with TestClient(create_app(orch=object())) as client:
        assert client.get("/api/usage/daily").status_code == 401
        response = client.get(
            "/api/usage/daily?days=1",
            headers={"Authorization": "Bearer " + TOKEN},
        )

    assert response.status_code == 200
    assert response.json() == {
        "days": [{"date": "2026-10-13", "channel": "web", "user_messages": 2}]
    }
    assert len(calls) == 1
    query_sql = " ".join(calls[0][0].lower().split())
    assert "role = 'user'" in query_sql
    assert "created_at >= %s" in query_sql
    assert "created_at < %s" in query_sql
    assert calls[0][1] == (
        datetime(2026, 10, 12, 22, tzinfo=UTC),
        datetime(2026, 10, 13, 22, tzinfo=UTC),
    )


def test_daily_usage_defaults_to_56_local_days(monkeypatch):
    calls = []
    monkeypatch.setattr(routers, "ROUTER_MODULES", [system])
    monkeypatch.setattr(config, "DASHBOARD_TOKEN", TOKEN, raising=False)
    monkeypatch.setattr(config, "DASHBOARD_ALLOWED_ORIGINS", "http://localhost:1420", raising=False)
    monkeypatch.setattr(system.db, "query", lambda _sql, params=(): calls.append(params) or [])

    with TestClient(create_app(orch=object())) as client:
        response = client.get(
            "/api/usage/daily",
            headers={"Authorization": "Bearer " + TOKEN},
        )

    assert response.status_code == 200
    assert response.json() == {"days": []}
    assert len(calls) == 1
    assert (calls[0][1] - calls[0][0]).days >= 55
