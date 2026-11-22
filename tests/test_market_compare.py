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
    card = dm.overview(mc.market_stats([match], 0.02), 0.04, track="<i>stats</i>")
    assert "Agreement with Pinnacle" not in card and "Signals (comparison" not in card
    assert "Statistics (all runs)" in card and "<i>stats</i>" in card


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
        dm.market_stats([], 0.02), 0.04, flow=dashboard.flow_html(summary, gaps)
    )


def test_fork_shows_parallel_reads_as_branches_from_pinnacle():
    import dashboard

    fork = dashboard.fork_html(
        {"pinnacle_age": 60.0, "pinnacle_captured": 1.0},
        [[(0.4, "Superbet"), (18.4, "Betclic")], [(0.8, "Polymarket")], [(None, "Kalshi")]],
    )
    branches = fork.split('<div class="flow-branch">')[1:]

    assert [b.count("flow-step") for b in branches] == [2, 1, 1]
    assert "Superbet" in branches[0] and "+0.4 s" in branches[0] and "+18.0 s" in branches[0]
    assert "+0.8 s" in branches[1]  # from Pinnacle, not from Superbet
    assert "Kalshi" in branches[2] and "No Kalshi prices" in branches[2]  # no data: last


def write_run(tmp_path, session_id, rows, markets):
    """A session CSV and its market file, as run_once and tennis_markets save them."""
    import csv
    import gzip

    sessions = tmp_path / "sessions"
    sessions.mkdir(exist_ok=True)
    with (sessions / f"{session_id}.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    folder = tmp_path / "markets" / "2026-11-20"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{session_id}.csv.gz"
    with gzip.open(path, "wt", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(markets[0]))
        writer.writeheader()
        writer.writerows(markets)
    return sessions, path


def test_market_signals_are_judged_like_the_track_record(tmp_path, monkeypatch):
    import dashboard
    import market_track as mt

    row = session_row(pinnacle_captured_at="2026-11-20T16:19:00Z", observation_id="obs-1")
    kalshi = market(source="kalshi", bid1=0.40, ask1=0.50, bid2=0.50, ask2=0.60)  # 10 cents wide
    sessions, path = write_run(tmp_path, "session_20261120_161900_aaaaaa", [row], [market(), kalshi])
    monkeypatch.setattr(mt, "SESSIONS_DIR", sessions)

    found = mt.market_sessions(tmp_path / "markets")
    assert list(found) == ["session_20261120_161900_aaaaaa"]
    cache = {}
    (candidate,) = mt.session_candidates("session_20261120_161900_aaaaaa", path, cache)
    assert candidate["Bet on"] == "Liam Broady" and candidate["_price"] == 2.30
    assert set(candidate["_markets"]) == {"Polymarket", "Kalshi"}
    assert mt.session_candidates("session_20261120_161900_aaaaaa", path, {**cache}) == [candidate]

    sources = {"sightings": {}, "by_day": {}, "covered": None, "signature": 1}
    now = datetime(2026, 11, 20, 18, tzinfo=timezone.utc)  # before the start: pending
    record = mt.market_record(dashboard, found, cache, sources, 0.02, 0.04, now)

    # Kalshi's spread is over 4 cents, so it and the average have no signal.
    assert list(record) == ["Polymarket"]
    stats = record["Polymarket"]
    assert stats["signals"] == 1 and stats["pending"] == 1
    assert stats["ev"] == pytest.approx(2.30 * 0.46 - 1)
    assert stats["agree"] == 1  # Pinnacle: 2.30 x 0.458 - 1 = +5.3%

    wide = mt.market_record(dashboard, found, cache, sources, 0.02, 0.10, now)
    assert list(wide) == ["Polymarket", "Kalshi", "Both"]
    assert wide["Both"]["ev"] == pytest.approx(2.30 * 0.45 - 1)  # the lower of the two

    card = dm.market_track_card(dashboard, wide, 0.02, None)
    assert card.count('class="track-book"') == 3 and "Agree w/ Pinnacle" in card
    assert "Loading" in dm.market_track_card(dashboard, None, 0.02, None)
    assert "No match has reached" in dm.market_track_card(dashboard, {}, 0.02, None)


def test_a_market_far_from_pinnacle_is_not_trusted():
    # A new Polymarket market at its opening 50/50 while Pinnacle says 30%: 20 pp
    # away, so greyed and no signal, while Kalshi (32%) still counts.
    row = session_row(probability1="0.30", probability2="0.70")
    poly = market(bid1=0.49, ask1=0.51, bid2=0.49, ask2=0.51)
    kalshi = market(source="kalshi", bid1=0.31, ask1=0.33, bid2=0.67, ask2=0.69)
    (match,) = mc.compare_session([row], [poly, kalshi], max_spread=0.04)

    assert match["markets"]["polymarket"]["off"] and not match["markets"]["kalshi"]["off"]
    assert set(mc.trusted_evs(match, 1)) == {"pinnacle", "kalshi"}
    record = dm.match_record(match, 0.02)
    assert record["_style Polymarket"] == dm.GREY
    assert "not a real price" in record.get("_title EV Polymarket", "")


def test_long_shots_and_far_prices_in_the_statistics():
    import market_track as mt

    def candidate(chance, pinnacle, price):
        return {"Bet on": "A", "Opponent": "B", "Tournament": "T", "_price": price,
                "_pinnacle": pinnacle, "_markets": {"Polymarket": (chance, 0.01)}}

    longshot = candidate(0.10, 0.06, 11.0)   # +10% against the market, a long shot
    placeholder = candidate(0.49, 0.04, 7.5)  # 45 pp from Pinnacle: not a price
    normal = candidate(0.50, 0.48, 2.10)

    picked = mt.flags_for([longshot, placeholder, normal], "Polymarket", 0.02, 0.04)
    assert [flag["_price"] for flag in picked] == [11.0, 2.10]
    picked = mt.flags_for([longshot, normal], "Polymarket", 0.02, 0.04, longshots=False)
    assert [flag["_price"] for flag in picked] == [2.10]
