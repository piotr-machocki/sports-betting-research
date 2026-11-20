"""Prediction-market tennis prices, kept for comparison only (scripts/tennis_markets.py)."""

import csv
import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import tennis_markets as tm  # noqa: E402

NOW = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


def poly_event(start="2026-11-20T11:30:00Z", **market):
    return {
        "slug": "atp-gulin-samuel-2026-11-19", "title": "Helsinki: Svyatoslav Gulin vs Toby Samuel",
        "seriesSlug": "atp", "startTime": start,
        "markets": [
            {"sportsMarketType": "tennis_first_set_winner", "outcomes": '["A", "B"]',
             "outcomePrices": '["0.3", "0.7"]', "bestBid": 0.2, "bestAsk": 0.4},
            {"sportsMarketType": "moneyline", "outcomes": '["Svyatoslav Gulin", "Toby Samuel"]',
             "outcomePrices": '["0.175", "0.825"]', "bestBid": 0.17, "bestAsk": 0.18,
             "volumeNum": 9061.1, "liquidityNum": 90556.6, **market},
        ],
    }


def test_polymarket_takes_the_moneyline_and_mirrors_the_book_for_player_2():
    (row,) = tm.parse_polymarket([poly_event()], NOW)

    assert (row["player1"], row["player2"]) == ("Svyatoslav Gulin", "Toby Samuel")
    assert (row["bid1"], row["ask1"], row["bid2"], row["ask2"]) == (0.17, 0.18, 0.82, 0.83)
    assert (row["last1"], row["last2"]) == (0.175, 0.825)
    assert row["tournament"] == "Helsinki"
    assert row["starts_at"] == "2026-11-20T11:30:00Z"


def test_polymarket_drops_old_unresolved_and_closed_markets():
    old = poly_event(start="2026-10-04T10:00:00Z")
    closed = poly_event(closed=True)
    no_moneyline = {**poly_event(), "markets": poly_event()["markets"][:1]}

    assert tm.parse_polymarket([old, closed, no_moneyline], NOW) == []


def kalshi_market(ticker, player, bid, ask, status="active"):
    return {
        "event_ticker": "KXATPCHALLENGERMATCH-26NOV20DIECHA", "ticker": ticker, "status": status,
        "yes_sub_title": player, "yes_bid_dollars": bid, "yes_ask_dollars": ask,
        "last_price_dollars": "0.0000", "volume_fp": "12.00",
        "rules_primary": f"If {player} wins the Dietrich vs Chan professional tennis match in the "
                         "2026 ATP Challenger Drummondville Quarterfinal after a ball has been played, ...",
    }


def test_kalshi_pairs_the_two_player_markets_of_an_event():
    markets = [
        kalshi_market("X-DIE", "Dylan Dietrich", "0.8000", "0.8200"),
        kalshi_market("X-CHA", "Eric Chan", "0.1800", "0.2000"),
        kalshi_market("Y-ONE", "Lonely Player", "0.5", "0.6") | {"event_ticker": "ALONE"},
    ]

    (row,) = tm.parse_kalshi(markets, "KXATPCHALLENGERMATCH", NOW)

    # Sorted by ticker, so the order is stable between runs.
    assert (row["player1"], row["player2"]) == ("Eric Chan", "Dylan Dietrich")
    assert (row["bid1"], row["ask1"], row["bid2"], row["ask2"]) == (0.18, 0.2, 0.8, 0.82)
    assert row["last1"] == ""  # no trade yet
    assert row["tournament"] == "2026 ATP Challenger Drummondville Quarterfinal"
    assert row["volume"] == 24.0


def test_one_failing_source_still_saves_the_other(monkeypatch, tmp_path):
    monkeypatch.setattr(tm, "OUT_DIR", tmp_path)
    monkeypatch.setattr(tm, "utc_now", lambda: NOW)

    def down(now):
        raise ConnectionError("gone")

    monkeypatch.setattr(tm, "SOURCES", {
        "polymarket": down,
        "kalshi": lambda now: tm.parse_kalshi(
            [kalshi_market("A", "P One", "0.4", "0.42"), kalshi_market("B", "P Two", "0.58", "0.6")],
            "KXATPMATCH", now,
        ),
    })

    assert tm.run("books_x") == 0

    with gzip.open(tmp_path / "2026-11-20" / "books_x.csv.gz", "rt", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    assert [row["source"] for row in rows] == ["kalshi"]


def test_no_rows_from_either_source_exits_1(monkeypatch, tmp_path):
    monkeypatch.setattr(tm, "OUT_DIR", tmp_path)
    monkeypatch.setattr(tm, "SOURCES", {"polymarket": lambda now: [], "kalshi": lambda now: []})

    assert tm.run("x") == 1
    assert not any(tmp_path.iterdir())
