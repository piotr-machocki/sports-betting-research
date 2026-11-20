"""Parsing of the PinnFeed reply (src/betting/books/pinnacle.py). No network."""

import pytest
import requests

from betting.books import pinnacle
from betting.books.pinnacle import CSV_FIELDS, parse_event, parse_pinnacle_matches_detailed

CAPTURED = "2026-11-20T10:40:00+00:00"


def moneyline(period, home, away):
    return {
        "type": "moneyline", "period": period, "main": True, "live": False,
        "outcomes": [{"name": "home", "decimal": home}, {"name": "away", "decimal": away}],
    }


def event(**overrides) -> dict:
    data = {
        "id": 1640720351,
        "league": {"id": 202412, "name": "ITF Women Monastir - QF", "group": "Tunisia"},
        "home": "Ela Nala Milic",
        "away": "Carla Giambelli",
        "startTime": "2026-11-20T12:00:00Z",
        "status": "pending",
        "live": False,
        "parentId": None,
        "units": "Sets",
        # Period 1 (first set) comes first in real replies.
        "markets": [moneyline(1, 2.01, 1.8197), moneyline(0, 2.12, 1.7407)],
    }
    data.update(overrides)
    return data


def test_match_winner_is_the_period_0_moneyline():
    match, reason = parse_event(event(), CAPTURED)

    assert reason is None
    assert (match["player1"], match["odds1"], match["player2"], match["odds2"]) == (
        "Ela Nala Milic", 2.12, "Carla Giambelli", 1.7407,
    )
    assert match["tournament"] == "ITF Women Monastir - QF"
    assert match["event_id"] == 1640720351
    assert match["starts_at"] == "2026-11-20T12:00:00Z"
    assert match["status"] == "prematch"
    assert set(match) == set(CSV_FIELDS)


def test_a_started_match_is_marked_live_so_no_ev_is_computed():
    match, _ = parse_event(event(status="started"), CAPTURED)

    assert match["status"] == "live"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"parentId": 1640720351, "units": "Games", "markets": []}, "child_event"),
        ({"markets": []}, "no_odds"),
        ({"markets": [moneyline(1, 2.0, 1.8)]}, "no_full_match_market"),
        ({"home": ""}, "missing_players"),
        ({"markets": [moneyline(0, None, None)]}, "no_money_line"),
        ({"markets": [moneyline(0, 1.0, 9.0)]}, "invalid_odds"),
        ({"markets": [moneyline(0, "x", 2.0)]}, "invalid_odds"),
    ],
)
def test_events_that_are_not_usable_matches_say_why(overrides, reason):
    assert parse_event(event(**overrides), CAPTURED) == (None, reason)


def test_reply_is_counted_per_reason():
    data = {"events": [event(), event(parentId=1, markets=[]), event(markets=[])]}

    matches, rejected = parse_pinnacle_matches_detailed(data, CAPTURED)

    assert len(matches) == 1
    assert rejected == {"child_event": 1, "no_odds": 1}


def test_key_comes_from_pinnfeed_key_only(monkeypatch):
    monkeypatch.delenv("PINNFEED_KEY", raising=False)
    monkeypatch.setenv("PINNAPI_KEY_TENNIS", "old")
    assert pinnacle.api_key() is None

    monkeypatch.setenv("PINNFEED_KEY", " abc ")
    assert pinnacle.api_key() == "abc"


class Reply:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


@pytest.mark.parametrize("status", [401, 403, 429])
def test_refusals_exit_at_once_without_retrying(monkeypatch, status):
    calls = []

    def get(*args, **kwargs):
        calls.append(kwargs)
        return Reply(status, {"error": "nope", "message": "plan limit"})

    monkeypatch.setattr(pinnacle.requests, "get", get)

    with pytest.raises(SystemExit) as exc:
        pinnacle.fetch_events("secret-key")

    assert len(calls) == 1
    assert str(status) in str(exc.value.code)
    assert "secret-key" not in str(exc.value.code)


def test_one_request_asks_for_every_prematch_tennis_moneyline(monkeypatch):
    seen = {}

    def get(url, params, headers, timeout):
        seen.update(url=url, params=params, headers=headers)
        return Reply(200, {"count": 0, "events": []}, {"X-Ratelimit-Remaining": "997", "X-Ratelimit-Limit": "1000"})

    monkeypatch.setattr(pinnacle.requests, "get", get)

    data, quota = pinnacle.fetch_events("k")

    assert seen["url"] == "https://pinnfeed.io/v1/odds"
    assert seen["params"] == {"sport": "tennis", "market": "moneyline", "live": "false", "limit": 1000}
    assert seen["headers"] == {"x-api-key": "k"}
    assert quota["remaining"] == "997"
