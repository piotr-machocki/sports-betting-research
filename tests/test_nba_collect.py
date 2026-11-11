import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import nba_collect as nc  # noqa: E402
from betting.nba_teams import game_date, team_code  # noqa: E402

NOW = datetime(2026, 11, 11, 22, 30, tzinfo=timezone.utc)


def test_team_codes_from_every_source_spelling():
    assert team_code("New York Knicks") == "NYK"
    assert team_code("Knicks") == "NYK"
    assert team_code("NY") == "NYK"  # ESPN
    assert team_code("Los Angeles C") == "LAC"  # Kalshi
    assert team_code("Trail Blazers") == "POR"
    assert team_code("76ers") == "PHI"
    assert team_code("UTAH") == "UTA"
    assert team_code("Los Angeles") is None  # two teams


def test_game_date_is_us_eastern():
    # 00:00 UTC on 12 Nov is 7 pm on 11 Nov in New York
    assert game_date(datetime(2026, 11, 12, 0, 0, tzinfo=timezone.utc)) == "2026-11-11"


def test_american_to_decimal():
    assert nc.american_to_decimal("+160") == 2.6
    assert nc.american_to_decimal(-200) == 1.5
    assert nc.american_to_decimal("50") is None


def test_pinnacle_spread_sign_and_totals():
    data = {"events": [{
        "event_id": 1, "league_name": "NBA", "parent_id": None,
        "home": "Memphis Grizzlies", "away": "Denver Nuggets",
        "starts": "2026-11-12T01:10:00Z",
        "periods": {"num_0": {"number": 0, "money_line": {"home": 2.49, "away": 1.61},
                              "spreads": {"4": {"hdp": 4, "home": 1.92, "away": 1.96, "max": 20000}},
                              "totals": {"230": {"points": 230, "over": 1.89, "under": 2.0}}}},
    }]}
    rows = nc.parse_pinnacle(data)
    keys = {(r["market"], r["side"], r.get("line", "")): r["price"] for r in rows}
    assert rows[0]["game_key"] == "2026-11-11_DEN@MEM"
    assert keys[("moneyline", "MEM", "")] == 2.49
    assert keys[("spread", "MEM", 4)] == 1.92
    assert keys[("spread", "DEN", -4)] == 1.96
    assert keys[("total", "under", 230)] == 2.0


def test_action_network_books_and_sides():
    data = {"games": [{
        "id": 9, "start_time": "2026-11-12T00:00:00.000Z",
        "home_team_id": 166, "away_team_id": 164,
        "teams": [{"id": 166, "abbr": "TOR", "full_name": "Toronto Raptors"},
                  {"id": 164, "abbr": "BOS", "full_name": "Boston Celtics"}],
        "markets": {"68": {"event": {
            "moneyline": [{"side": "home", "odds": 184, "is_live": False},
                          {"side": "away", "odds": -222, "is_live": False}],
            "total": [{"side": "over", "odds": -110, "value": 215.5, "is_live": False},
                      {"side": "under", "odds": -110, "value": 215.5, "is_live": True}],
        }}},
    }]}
    rows = nc.parse_action(data)
    assert {(r["book"], r["market"], r["side"]) for r in rows} == {
        ("DraftKings", "moneyline", "TOR"), ("DraftKings", "moneyline", "BOS"),
        ("DraftKings", "total", "over"),
    }
    assert all(r["game_key"] == "2026-11-11_BOS@TOR" for r in rows)


def test_espn_results_and_open_close():
    data = {"events": [
        {"id": "1", "date": "2026-11-11T00:00Z",
         "status": {"type": {"completed": True, "state": "post", "name": "STATUS_FINAL"}},
         "competitions": [{"competitors": [
             {"homeAway": "home", "team": {"abbreviation": "NY"}, "score": "110"},
             {"homeAway": "away", "team": {"abbreviation": "CLE"}, "score": "101"}]}]},
        {"id": "2", "date": "2026-11-12T00:00Z",
         "status": {"type": {"completed": False, "state": "pre"}},
         "competitions": [{"competitors": [
             {"homeAway": "home", "team": {"abbreviation": "IND"}},
             {"homeAway": "away", "team": {"abbreviation": "WSH"}}],
             "odds": [{"provider": {"name": "DraftKings"}, "moneyline": {
                 "home": {"close": {"odds": "-150"}, "open": {"odds": "-140"}},
                 "away": {"close": {"odds": "+130"}, "open": {"odds": "+120"}}}}]}]},
    ]}
    rows, results = nc.parse_espn(data, NOW)
    assert results[0]["winner"] == "NYK" and results[0]["game_key"] == "2026-11-10_CLE@NYK"
    prices = {(r["book"], r["side"]): r["price"] for r in rows}
    assert prices[("DraftKings", "WAS")] == 2.3
    assert prices[("DraftKings open", "IND")] == round(1 + 100 / 140, 4)


def test_kalshi_tickers():
    assert nc.kalshi_game("KXNBAGAME-26NOV15SASUTA") == ("2026-11-15", "SAS", "UTA")
    markets = [
        {"event_ticker": "KXNBASPREAD-26NOV11ORLUTA", "ticker": "KXNBASPREAD-26NOV11ORLUTA-UTA8",
         "floor_strike": 7.5, "yes_bid_dollars": "0.19", "yes_ask_dollars": "0.22"},
    ]
    (row,) = nc.parse_kalshi(markets, "spread")
    assert (row["side"], row["line"], row["bid"], row["ask"]) == ("UTA", -7.5, 0.19, 0.22)
    props = [{"event_ticker": "KXNBAPTS-26NOV11OKCPOR", "ticker": "KXNBAPTS-26NOV11OKCPOR-X-20",
              "title": "Aday Mara: 20+ points", "floor_strike": 19.5,
              "yes_bid_dollars": "0.03", "yes_ask_dollars": "0.12"}]
    (row,) = nc.parse_kalshi(props, "player_points")
    assert (row["player"], row["side"], row["line"], row["game_key"]) == (
        "Aday Mara", "over", 19.5, "2026-11-11_OKC@POR")


def test_polymarket_quotes_for_both_outcomes():
    markets = [
        {"id": "1", "sportsMarketType": "spreads", "question": "Spread: Knicks (-3.5)",
         "outcomes": json.dumps(["Knicks", "Cavaliers"]), "outcomePrices": json.dumps(["0.535", "0.465"]),
         "bestBid": 0.53, "bestAsk": 0.54, "gameStartTime": "2026-11-12 00:00:00+00",
         "events": [{"slug": "nba-cle-nyk-2026-11-11"}]},
        {"id": "2", "sportsMarketType": "points", "question": "Jalen Brunson: Points O/U 27.5",
         "line": 27.5, "outcomes": json.dumps(["Yes", "No"]), "outcomePrices": json.dumps(["0.4", "0.6"]),
         "bestBid": 0.39, "bestAsk": 0.41, "events": [{"slug": "nba-cle-nyk-2026-11-11"}]},
    ]
    rows = nc.parse_polymarket(markets)
    spread = {r["side"]: r for r in rows if r["market"] == "spread"}
    assert spread["NYK"]["line"] == -3.5 and spread["CLE"]["line"] == 3.5
    assert spread["CLE"]["bid"] == 1 - 0.54 and spread["CLE"]["ask"] == 1 - 0.53
    props = {r["side"]: r for r in rows if r["market"] == "player_points"}
    assert props["over"]["player"] == "Jalen Brunson" and props["over"]["line"] == 27.5
    assert spread["NYK"]["starts_at"] == "2026-11-12T00:00:00Z"


def test_fill_starts_and_prematch_filter():
    rows = [
        {"source": "kalshi", "game_key": "g1", "game_date": "2026-11-11", "starts_at": ""},
        {"source": "espn", "game_key": "g1", "game_date": "2026-11-11", "starts_at": "2026-11-12T00:00:00Z"},
        {"source": "kalshi", "game_key": "g2", "game_date": "2026-11-11", "starts_at": "2026-11-11T20:00:00Z"},
        {"source": "kalshi", "game_key": "g3", "game_date": "2026-11-10", "starts_at": ""},
    ]
    nc.fill_starts(rows)
    assert rows[0]["starts_at"] == "2026-11-12T00:00:00Z"
    kept = nc.prematch_only(rows, NOW)
    assert [r["game_key"] for r in kept] == ["g1", "g1"]


def test_props_only_in_first_run_of_the_hour():
    assert nc.want_props("auto", NOW.replace(minute=7))
    assert not nc.want_props("auto", NOW.replace(minute=22))
    assert nc.want_props("always", NOW) and not nc.want_props("never", NOW.replace(minute=7))


def _espn_item(type_name, athlete, value, decimal, open_value=None, open_decimal=None, target=None):
    return {
        "athlete": {"$ref": f"http://x/athletes/{athlete}?lang=en"},
        "type": {"name": type_name},
        "odds": {"decimal": {"value": str(decimal), "open": str(open_decimal or decimal)},
                 "total": {"value": str(value), "open": str(open_value or value)}},
        "current": {"target": {"value": target}},
    }


def test_espn_props_pairs_over_first_and_milestones():
    items = [
        _espn_item("Total Points", "7", 22.5, 1.84),
        _espn_item("Total Points", "7", 22.5, 1.92, open_decimal=1.95),
        _espn_item("Points Milestones", "7", "25+", 2.3, target=25.0),
        _espn_item("Total Points", "8", 10.5, 1.9),  # unpaired: skipped
        _espn_item("First Basket", "7", "", 9.0),
    ]
    rows = nc.parse_espn_props(items, {"7": "Anthony Davis", "8": "X"}, "WAS", "IND", None, "1")
    got = {(r["book"], r["side"], r["line"]): r["price"] for r in rows}
    assert got == {
        ("DraftKings", "over", 22.5): 1.84, ("DraftKings", "under", 22.5): 1.92,
        ("DraftKings", "over", 24.5): 2.3,
        ("DraftKings open", "under", 22.5): 1.95,  # open kept only where it differs
    }
    assert {r["player"] for r in rows} == {"Anthony Davis"}


SAO_LIST = (
    '<a href="/prop-bets/408971/luka-doncic">Luka Doncic</a><br />'
    ' <span class="bold small gray">LAL @ MIL</span>'
)
SAO_PAGE = """
<table><thead><tr><th>x</th>
<th class="book-logo"><img alt="draftkings"/></th><th class="book-logo"><img alt="fanduel"/></th>
<th class="book-logo"><img alt="kalshi"/></th></tr></thead>
<tbody data-key="odds-table--points-p-236739780">
<tr data-name="luka doncic"><td>Luka</td>
<td class="game-odds"><span class="data-value"> o34.5 </span><small class="data-odds">-117</small></td>
<td class="game-odds"><span class="data-value"> o34.5 </span><small class="data-odds">even</small></td>
<td class="game-odds"></td></tr>
<tr data-name="luka doncic"><td>Luka</td>
<td class="game-odds"><span class="data-value"> u34.5 </span><small class="data-odds">-109</small></td>
<td class="game-odds"><span class="data-value"> u34.5 </span><small class="data-odds">-120</small></td>
<td class="game-odds"></td></tr>
</tbody></table>
"""


def test_scoresandodds_list_and_player_page():
    assert nc.sao_players(SAO_LIST) == [("408971", "luka-doncic", "Luka Doncic", "LAL", "MIL")]
    rows = nc.parse_sao_player(SAO_PAGE, "Luka Doncic", "LAL", "MIL", "408971")
    got = {(r["book"], r["side"], r["line"]): r["price"] for r in rows}
    assert got == {
        ("DraftKings", "over", 34.5): round(1 + 100 / 117, 4), ("FanDuel", "over", 34.5): 2.0,
        ("DraftKings", "under", 34.5): round(1 + 100 / 109, 4), ("FanDuel", "under", 34.5): round(1 + 100 / 120, 4),
    }
    assert {r["market"] for r in rows} == {"player_points"}


def test_resolve_dates_uses_other_sources():
    rows = [
        {"away": "LAL", "home": "MIL", "game_date": "2026-11-12", "game_key": "2026-11-12_LAL@MIL"},
        {"away": "LAL", "home": "MIL", "game_date": "", "game_key": "_LAL@MIL"},
        {"away": "BOS", "home": "TOR", "game_date": "", "game_key": "_BOS@TOR"},
    ]
    nc.resolve_dates(rows, NOW)
    assert rows[1]["game_key"] == "2026-11-12_LAL@MIL"
    assert rows[2]["game_key"] == "2026-11-11_BOS@TOR"  # today's US date
