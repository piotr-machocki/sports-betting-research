"""Prediction markets against a session's matches (scripts/market_compare.py) and the
dashboard's Prediction markets subtab (scripts/dashboard_markets.py)."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import dashboard_markets as dm  # noqa: E402
import market_compare as mc  # noqa: E402
from betting.margin import remove_margin  # noqa: E402

CAPTURED = "2026-11-20T16:20:00Z"


def session_row(**extra):
    row = {
        "pinnacle_player1": "Liam Broady", "pinnacle_player2": "Jay Dylan Hara Friend",
        "pinnacle_tournament": "ATP Challenger Kobe - SF",
        "pinnacle_starts_at": "2026-11-21T01:00:00Z",
        "pinnacle_odds1": "2.10", "pinnacle_odds2": "1.80",
        "probability1": "0.458", "probability2": "0.542",
        "ev1": "-0.03", "ev2": "-0.04",
        "betclic_odds_for_pin_p1": "2.30", "betclic_odds_for_pin_p2": "1.70",
        "status": "FOUND", "betclic_status": "",
    }
    row.update(extra)
    return row


def market(source="polymarket", p1="Liam Broady", p2="Jay Friend", bid1=0.45, ask1=0.47,
           bid2=0.53, ask2=0.55, starts="2026-11-21T01:00:00Z"):
    return {
        "source": source, "captured_at": CAPTURED, "event_id": f"{source}-1",
        "starts_at": starts if source == "polymarket" else "",
        "player1": p1, "player2": p2,
        "bid1": bid1, "ask1": ask1, "bid2": bid2, "ask2": ask2,
        "volume": 1000, "liquidity": 5000,
    }


@pytest.mark.parametrize("first, second, expected", [
    ("Jay Dylan Hara Friend", "Jay Friend", True),          # extra middle names
    ("Caijsa Hennemann", "Caijsa Wilda Hennemann", True),
    ("Fanming Meng", "Meng Fanming", True),                 # reversed order
    ("Nauhany Vitoria Leme Da Silva", "Leme Da Silva", True),
    ("Inaki Montes-De La Torre", "Inaki Montes", True),
    ("Joao Ribeiro", "Miguel Ribeiro", False),              # same surname, other player
    ("Liam Broady", "Liam Draxl", False),                   # given name alone is not enough
    ("Elmer Möller", "Elmer Moller", True),                 # accents
])
def test_same_person(first, second, expected):
    assert mc.same_person(first, second) is expected


def test_doubles_match_surname_only_names_in_either_partner_order():
    assert mc.same_team("Simone Bolelli / Andrea Vavassori", "Vavassori/Bolelli")
    assert not mc.same_team("Simone Bolelli / Andrea Vavassori", "Bolelli/Krawietz")
    assert not mc.same_team("Simone Bolelli / Andrea Vavassori", "Simone Bolelli")


def test_reversed_market_is_turned_to_pinnacle_order():
    row = session_row()
    hit = mc.find_market(row, [market(p1="Jay Friend", p2="Liam Broady",
                                      bid1=0.53, ask1=0.55, bid2=0.45, ask2=0.47)])
    assert hit is not None and hit[1] == 2
    view = mc.market_view(*hit)
    assert view["probability1"] == pytest.approx(0.46)
    assert view["sides"][1]["ask"] == 0.47


def test_polymarket_start_must_be_within_a_day_of_pinnacle():
    row = session_row()
    assert mc.find_market(row, [market(starts="2026-11-23T01:00:00Z")]) is None
    # Kalshi has no start, so names alone decide.
    assert mc.find_market(row, [market(source="kalshi")]) is not None


def test_kalshi_middles_are_scaled_to_add_up_to_one():
    view = mc.market_view(market(source="kalshi", bid1=0.45, ask1=0.47, bid2=0.55, ask2=0.57), 1)
    assert view["probability1"] == pytest.approx(0.46 / (0.46 + 0.56))
    assert view["spread"] == pytest.approx(0.02)


def test_one_side_without_quotes_takes_the_complement():
    view = mc.market_view(market(source="kalshi", bid2="", ask2=""), 1)
    assert view["probability1"] == pytest.approx(0.46)


def test_power_devig_matches_the_margin_module():
    for odds in [(2.10, 1.80), (1.05, 11.0), (1.2924, 3.6)]:
        assert mc.power_devig(*odds)[0] == pytest.approx(remove_margin(list(odds), "power")[0], abs=1e-7)


def test_compare_row_evs_against_each_reference():
    snapshot = [market(), market(source="kalshi", bid1=0.40, ask1=0.50, bid2=0.50, ask2=0.60)]
    (match,) = mc.compare_session([session_row()], snapshot, max_spread=0.04)

    side1 = match["sides"][1]
    assert side1["book"] == "Betclic" and side1["price"] == 2.30
    assert side1["evs"]["pinnacle"] == pytest.approx(2.30 * 0.458 - 1)
    assert side1["evs"]["polymarket"] == pytest.approx(2.30 * 0.46 - 1)
    # Kalshi's 10-cent spread is shown but not trusted.
    assert not match["markets"]["kalshi"]["tight"]
    assert set(mc.trusted_evs(match, 1)) == {"pinnacle", "polymarket"}
    assert mc.agreement(match, 1, 0.02)
    assert mc.best_side(match) == 1


def test_superbet_is_priced_after_tax_and_betclic_below_its_minimum_is_left_out():
    row = session_row(betclic_odds_for_pin_p2="1.10", superbet_status="FOUND",
                      superbet_price1="2.50", superbet_price2="1.60")
    prices = mc.book_prices(row, 1)
    assert prices == {"Betclic": 2.30, "Superbet": pytest.approx(2.20)}
    assert "Betclic" not in mc.book_prices(row, 2)


def test_betclic_needs_the_workers_ev():
    assert mc.book_prices(session_row(ev1="", ev2="", betclic_status="live"), 1) == {}


def test_started_matches_get_no_ev_and_stay_out_of_the_stats():
    live = session_row(pinnacle_starts_at="2026-11-20T16:00:00Z", probability1="", ev1="", ev2="")
    matches = mc.compare_session([session_row(), live], [market()], max_spread=0.04)

    assert [m["started"] for m in matches] == [False, True]
    assert matches[1]["sides"][1]["evs"]["polymarket"] is None
    # Pinnacle's probability is devigged here when the worker left it blank.
    assert matches[1]["sides"][1]["pinnacle"] == pytest.approx(mc.power_devig(2.10, 1.80)[0])

    stats = mc.market_stats(matches, 0.02)
    assert (stats["matches"], stats["started"]) == (1, 1)
    assert stats["sources"]["polymarket"]["matched"] == 1
    assert stats["sources"]["polymarket"]["median_abs_gap"] == pytest.approx(0.002)
    assert stats["agreeing"] == 1
    assert stats["books"] == {"Betclic": 1, "Superbet": 0}   # started match left out


def test_snapshot_path_finds_the_next_days_folder(tmp_path):
    folder = tmp_path / "2026-11-21"
    folder.mkdir()
    path = folder / "session_20261120_235502_abcdef.csv.gz"
    path.write_bytes(b"")
    assert mc.snapshot_path("session_20261120_235502_abcdef", tmp_path) == path
    assert mc.latest_snapshots(tmp_path) == [path]


def test_dashboard_record_and_table():
    snapshot = [market(), market(source="kalshi", bid1=0.40, ask1=0.50, bid2=0.50, ask2=0.60)]
    (match,) = mc.compare_session([session_row()], snapshot, max_spread=0.04)
    record = dm.match_record(match, 0.02)

    assert record["Bet on"] == "Liam Broady" and record["Book"] == "Betclic"
    assert record["Polymarket"] == "46.0%" and record["EV Polymarket"] == "+5.8%"
    assert record["_style EV Kalshi"] == dm.GREY        # wide spread
    assert record["Agree"] == dm.CHECK
    html = dm.table_html([record])
    assert 'class="matches markets"' in html and "Jay Dylan Hara Friend" in html
    assert "Agreement with Pinnacle" in dm.overview(mc.market_stats([match], 0.02), 0.02)


def test_coverage_card_lists_pinnacle_books_and_markets():
    (match,) = mc.compare_session([session_row()], [market()], max_spread=0.04)
    card = dm.meters(mc.market_stats([match], 0.02))
    labels = [part.split("</div>")[0] for part in card.split("<div>")[1:]]
    assert labels == ["Pinnacle", "Polymarket", "Kalshi", "Betclic", "Superbet"]
    assert "1 of 1 matches priced on Betclic" in card
    assert "1 prematch matches on Pinnacle&#39;s list" in card
    stats = mc.market_stats([match], 0.02)
    stats["sources"]["kalshi"].update(matched=35, tight=32)
    stats["matches"] = 38
    card = dm.meters(stats)
    assert '35 <span class="overview-note has-tip" tabindex="0">(3)' in card
    assert "3 of 35 with a spread wider than the limit (4 cents)." in card
    assert "(7 cents)." in dm.meters(stats, 0.07)
    assert "</span></span> / 38" in card
    assert "1 / 38" in card.split("<div>Betclic</div>")[1]


def test_superbet_price_is_shown_as_the_app_shows_it():
    row = session_row(betclic_odds_for_pin_p1="", ev1="", ev2="", superbet_status="FOUND",
                      superbet_price1="2.50", superbet_price2="1.60")
    (match,) = mc.compare_session([row], [market()], max_spread=0.04)
    record = dm.match_record(match, 0.02)
    assert (record["Book"], record["Price"]) == ("Superbet", "2.50")


def test_age_text():
    now = datetime(2026, 11, 20, 16, 25, tzinfo=timezone.utc)
    assert dm.age_text(datetime(2026, 11, 20, 16, 20, 55, tzinfo=timezone.utc), now) == "4 min 5 s ago"


def test_flow_names_each_market_after_pinnacle():
    from datetime import datetime, timezone

    import dashboard

    rows = [
        {"pinnacle_captured_at": "2026-11-22T13:10:00Z",
         "superbet_captured_at": "2026-11-22T13:10:01Z", "betclic_captured_at": "2026-11-22T13:10:10Z"},
        {"pinnacle_captured_at": "2026-11-22T13:10:02Z",
         "superbet_captured_at": "2026-11-22T13:10:03Z", "betclic_captured_at": "2026-11-22T13:10:12Z"},
    ]
    snapshot = [
        {"source": "polymarket", "captured_at": "2026-11-22T13:11:01Z"},
        {"source": "kalshi", "captured_at": "2026-11-22T13:11:03Z"},
    ]
    now = datetime(2026, 11, 22, 13, 12, tzinfo=timezone.utc)
    summary, gaps = dm.flow_summary(rows, snapshot, now)

    assert summary["pinnacle_age"] == 120.0
    assert gaps == [(10.0, "Betclic"), (1.0, "Superbet"), (60.0, "Polymarket"), (62.0, "Kalshi")]
    flow = dashboard.flow_html(summary, gaps)
    names = ["Superbet", "Betclic", "Polymarket", "Kalshi"]
    assert sorted(names, key=flow.index) == names  # in the order the prices came
    assert "+9.0 s" in flow and "+50.0 s" in flow
    assert "+2.0 s" in flow

    summary, gaps = dm.flow_summary(rows, snapshot[:1], now)
    assert gaps[3] == (None, "Kalshi") and "No Kalshi prices" in dashboard.flow_html(summary, gaps)
    assert "Data flow and age" in dm.overview(
        dm.market_stats([], 0.02), 0.02, flow=dashboard.flow_html(summary, gaps)
    )
