"""Tennis set and games markets, logged for a later EV study (scripts/tennis_props.py)."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import tennis_props as tp  # noqa: E402

NOW = datetime(2026, 11, 25, 21, 0, tzinfo=timezone.utc)
MATCH = {"pinnacle_event_id": 1641066481, "starts_at": "2026-11-25T22:00:00Z",
         "tournament": "ITF Men Santa Cruz - R1", "player1": "Valentin Guadagno",
         "player2": "Ivan Biletic"}


def outcome(name, decimal, points=None):
    return {"name": name, "decimal": decimal, "points": points}


def market(kind, period, *outcomes, alternate=False):
    return {"id": f"{kind}{period}", "type": kind, "period": period, "alternate": alternate,
            "maxStake": 450, "outcomes": list(outcomes)}


def pinnfeed(start="2026-11-25T22:00:00Z", status="pending"):
    parent = {
        "id": 1641066481, "parentId": None, "home": "Valentin Guadagno", "away": "Ivan Biletic",
        "league": {"name": "ITF Men Santa Cruz - R1"}, "startTime": start, "status": status,
        "live": False, "markets": [
            market("moneyline", 0, outcome("home", 4.92), outcome("away", 1.184)),
            market("moneyline", 1, outcome("home", 3.87), outcome("away", 1.265)),
            market("spread", 0, outcome("home", 2.57, 1.5), outcome("away", 1.518, -1.5)),
            market("total", 0, outcome("over", 3.51, 2.5), outcome("under", 1.309, 2.5)),
        ],
    }
    child = {
        "id": 1641080006, "parentId": 1641066481, "home": "Valentin Guadagno (Games)",
        "away": "Ivan Biletic (Games)", "startTime": start, "status": status, "live": False,
        "markets": [
            market("spread", 0, outcome("home", 1.99, 5.5), outcome("away", 1.84, -5.5)),
            market("total", 0, outcome("over", 1.917, 19), outcome("under", 1.9, 19)),
            market("total", 1, outcome("over", 1.625, 8.5), outcome("under", 2.32, 8.5)),
            market("total", 0, outcome("over", 1.5, 20.5), outcome("under", 2.5, 20.5),
                   alternate=True),
        ],
    }
    return {"events": [child, parent]}


def test_pinnacle_keys_every_market_to_the_match_in_its_player_order():
    matches, rows = tp.parse_pinnacle(pinnfeed(), NOW)

    assert matches == [MATCH]
    got = {(r["market"], r["side"], r["line"]): r["price"] for r in rows}
    assert got[("match_winner", "p1", "")] == 4.92
    assert got[("set1_winner", "p2", "")] == 1.265
    assert got[("set_handicap", "p2", -1.5)] == 1.518
    assert got[("total_sets", "over", 2.5)] == 3.51
    assert got[("game_handicap", "p1", 5.5)] == 1.99
    assert got[("total_games", "under", 19)] == 1.9
    assert got[("set1_total_games", "over", 8.5)] == 1.625
    assert ("total_games", "over", 20.5) not in got  # alternate lines are left out
    assert {r["player1"] for r in rows} == {"Valentin Guadagno"}


def test_pinnacle_leaves_out_started_and_far_matches():
    assert tp.parse_pinnacle(pinnfeed(status="started"), NOW) == ([], [])
    assert tp.parse_pinnacle(pinnfeed(start="2026-11-25T20:00:00Z"), NOW) == ([], [])
    assert tp.parse_pinnacle(pinnfeed(start="2026-11-28T20:00:00Z"), NOW) == ([], [])


def poly(kind, outcomes, line=None, bid=0.4, ask=0.45, **extra):
    return {"id": 7, "sportsMarketType": kind, "outcomes": json.dumps(outcomes), "line": line,
            "bestBid": bid, "bestAsk": ask, "liquidityNum": 500, **extra}


def test_polymarket_handicap_line_belongs_to_the_first_outcome_and_sides_follow_pinnacle():
    rows = tp.poly_market_rows(
        poly("tennis_game_handicap", ["Ivan Biletic", "Valentin Guadagno"], line=-5.5), MATCH)

    assert [(r["side"], r["line"], r["bid"], r["ask"]) for r in rows] == [
        ("p2", -5.5, 0.4, 0.45), ("p1", 5.5, 0.55, 0.6)]
    assert {r["book"] for r in rows} == {"Polymarket"}


def test_polymarket_totals_set_winners_and_empty_books():
    total = tp.poly_market_rows(poly("tennis_match_totals", ["Over", "Under"], line=21.5), MATCH)
    assert [(r["market"], r["side"], r["line"]) for r in total] == [
        ("total_games", "over", 21.5), ("total_games", "under", 21.5)]

    second = tp.poly_market_rows(
        poly("tennis_set_winner", ["Valentin Guadagno", "Ivan Biletic"], line=2), MATCH)
    assert {r["market"] for r in second} == {"set2_winner"}

    # No bid and nobody selling: nothing to store.
    assert tp.poly_market_rows(
        poly("tennis_set_totals", ["Over 2.5", "Under 2.5"], line=2.5, bid=None, ask=1), MATCH) == []
    # Another pairing of names is not this match.
    assert tp.poly_market_rows(poly("moneyline", ["Someone Else", "Ivan Biletic"]), MATCH) == []


def test_polymarket_events_are_matched_on_names_and_start():
    event = {"startTime": "2026-11-25T22:05:00Z", "markets": [
        poly("moneyline", ["Valentin Guadagno", "Ivan Biletic"]),
        poly("tennis_first_set_winner", ["Valentin Guadagno", "Ivan Biletic"]),
    ]}
    rows = tp.parse_polymarket([event], [MATCH])
    assert {r["market"] for r in rows} == {"match_winner", "set1_winner"}

    far = {**event, "startTime": "2026-11-28T22:05:00Z"}
    assert tp.parse_polymarket([far], [MATCH]) == []


def test_kalshi_set_winners_are_paired_per_event():
    def contract(player, bid, ask, set_no=1):
        return {"event_ticker": f"KXATPCHALLENGERSETWINNER-26NOV25GUABIL-{set_no}",
                "status": "active", "title": f"{player} wins Set {set_no}",
                "yes_sub_title": player, "yes_bid_dollars": bid, "yes_ask_dollars": ask,
                "volume_fp": 10}

    rows = tp.parse_kalshi([contract("Ivan Biletic", "0.70", "0.74"),
                            contract("Valentin Guadagno", "0.25", "0.29")], [MATCH])
    assert [(r["market"], r["side"], r["bid"], r["ask"]) for r in rows] == [
        ("set1_winner", "p2", 0.7, 0.74), ("set1_winner", "p1", 0.25, 0.29)]


def test_link_session_takes_the_newest_finished_recent_one(tmp_path):
    def status(name, state, start):
        (tmp_path / f"{name}_status.json").write_text(json.dumps(
            {"session_id": name, "status": state, "session_start": start}), encoding="utf-8")

    status("session_20261125_204502_aaaaaa", "finished", "2026-11-25T20:45:02Z")
    status("session_20261125_205002_bbbbbb", "running", "2026-11-25T20:50:02Z")
    assert tp.link_session(NOW, tmp_path) == "session_20261125_204502_aaaaaa"
    # Too old.
    assert tp.link_session(datetime(2026, 11, 25, 22, 0, tzinfo=timezone.utc), tmp_path) is None


def test_private_rows_get_the_match_fields(tmp_path, monkeypatch):
    collector = tmp_path / "fake_props.py"
    collector.write_text(
        "def collect(now, matches, session_id):\n"
        "    return [{'pinnacle_event_id': 1641066481, 'source': 'superbet', 'book': 'Superbet',\n"
        "             'market': 'total_games', 'side': 'over', 'line': 19.5, 'price': 1.9},\n"
        "            {'pinnacle_event_id': 1, 'source': 'superbet', 'book': 'Superbet',\n"
        "             'market': 'total_games', 'side': 'over', 'line': 19.5, 'price': 1.9}]\n",
        encoding="utf-8")
    monkeypatch.setattr(tp, "ROOT", tmp_path)
    rows = tp.run_private(collector, NOW, [MATCH], "session_x")
    assert len(rows) == 1  # a row for a match not on Pinnacle's list is dropped
    assert rows[0]["player2"] == "Ivan Biletic" and rows[0]["price"] == 1.9
