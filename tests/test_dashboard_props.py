"""The Props subtab: books' set and games prices against Pinnacle (scripts/dashboard_props.py)."""

import csv
import gzip
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import dashboard_props as dp  # noqa: E402
import tennis_props  # noqa: E402

BASE = {"pinnacle_event_id": "9", "starts_at": "2026-11-25T22:00:00Z",
        "tournament": "ITF", "player1": "Solana Sierra", "player2": "Carole Monnet",
        "captured_at": "2026-11-25T21:00:00Z"}


def row(source, market, side, line="", price="", **extra):
    book = {"pinnacle": "Pinnacle", "superbet": "Superbet", "betclic": "Betclic",
            "polymarket": "Polymarket"}[source]
    return {**BASE, "source": source, "book": book, "market": market, "side": side,
            "line": line, "price": price, **extra}


def game_handicap_rows(betclic_price="2.20"):
    return [
        row("pinnacle", "game_handicap", "p1", "-3.5", "2.00"),
        row("pinnacle", "game_handicap", "p2", "3.5", "1.90"),
        # Monnet +3.5 is the same line as Sierra -3.5.
        row("betclic", "game_handicap", "p2", "3.5", betclic_price),
        row("superbet", "game_handicap", "p1", "-3.5", "2.30"),
        row("superbet", "game_handicap", "p1", "-4.5", "2.80"),  # no Pinnacle line
        row("polymarket", "game_handicap", "p2", "3.5", bid="0.50", ask="0.54"),
    ]


def test_handicaps_meet_on_player_one_line():
    assert dp.key_line({"market": "game_handicap", "side": "p2", "line": "3.5"}) == "-3.5"
    assert dp.key_line({"market": "total_games", "side": "under", "line": "19"}) == "19"
    assert dp.key_line({"market": "set1_winner", "side": "p2", "line": ""}) == ""


def test_offers_price_each_book_against_pinnacle_on_the_same_line():
    found = {(o["book"], o["side"]): o for o in dp.offers(game_handicap_rows())}

    assert set(found) == {("Betclic", "p2"), ("Superbet", "p1")}
    p1, p2 = found["Superbet", "p1"]["probability"], found["Betclic", "p2"]["probability"]
    assert p1 + p2 == pytest.approx(1)
    assert p2 > p1  # 1.90 is the shorter price
    # Superbet after its tax, Betclic as shown.
    assert found["Superbet", "p1"]["ev"] == pytest.approx(2.30 * 0.88 * p1 - 1)
    assert found["Betclic", "p2"]["ev"] == pytest.approx(2.20 * p2 - 1)
    assert found["Betclic", "p2"]["exchanges"] == {"Polymarket": pytest.approx(0.52)}


def test_betclic_prices_below_its_minimum_are_not_bets():
    found = dp.offers(game_handicap_rows(betclic_price="1.10"))
    assert {o["book"] for o in found} == {"Superbet"}


def test_market_summary_counts_matches_and_signals():
    rows = game_handicap_rows()
    summary = {s["market"]: s for s in dp.market_summary(rows, dp.offers(rows), 0.02)}
    handicap = summary["game_handicap"]
    assert handicap["pinnacle"] == 1
    assert handicap["books"]["Betclic"]["matches"] == 1
    assert summary["total_games"]["pinnacle"] == 0


def test_bet_text():
    offer = {"player1": "Solana Sierra", "player2": "Carole Monnet / Ana Lopez"}
    assert dp.bet_text({**offer, "market": "game_handicap", "side": "p1", "line": -3.5}) == "Sierra -3.5 games"
    assert dp.bet_text({**offer, "market": "set_handicap", "side": "p2", "line": 1.5}) == "Monnet / Lopez +1.5 sets"
    assert dp.bet_text({**offer, "market": "set1_total_games", "side": "over", "line": 9.5}) == "Set 1: Over 9.5 games"
    assert dp.bet_text({**offer, "market": "set1_winner", "side": "p1", "line": None}) == "Sierra wins set 1"


def write_run(folder: Path, name: str, rows: list[dict]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with gzip.open(folder / name, "wt", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=tennis_props.ROW_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def at(rows, captured):
    return [{**r, "captured_at": captured} for r in rows]


def test_history_keeps_the_first_flag_and_the_last_pinnacle_price_before_the_start(tmp_path):
    folder = tmp_path / "snapshots" / "2026-11-25"
    rows = game_handicap_rows()
    write_run(folder, "props_20261125_200000.csv.gz", at(rows, "2026-11-25T20:00:00Z"))
    # Pinnacle moves towards Monnet +3.5 before the start: the close.
    later = [dict(r) for r in rows]
    later[0]["price"], later[1]["price"] = "2.40", "1.60"
    write_run(folder, "props_20261125_213000.csv.gz", at(later, "2026-11-25T21:30:00Z"))
    # After the start nothing counts any more.
    write_run(folder, "props_20261125_221500.csv.gz", at(rows, "2026-11-25T22:15:00Z"))

    store = dp.HistoryStore(tmp_path / "cache.pkl", tmp_path / "snapshots")
    history = store.update()
    assert history["runs"] == 3

    before = dp.history_flags(history, 0.02, datetime(2026, 11, 25, 21, 0, tzinfo=timezone.utc))
    after = dp.history_flags(history, 0.02, datetime(2026, 11, 25, 23, 0, tzinfo=timezone.utc))
    betclic = next(f for f in after if f["book"] == "Betclic")
    assert betclic["captured_at"] == "2026-11-25T20:00:00Z"
    _, close = dp.market_compare.power_devig(2.40, 1.60)
    assert betclic["clv"] == pytest.approx(2.20 * close - 1)
    assert all(f["clv"] is None for f in before)  # not started yet

    stats = dp.history_stats(after, "Betclic")
    assert stats["signals"] == 1 and stats["with_close"] == 1 and stats["clv_positive"] == 1

    # Saved and read back; a second update adds nothing.
    again = dp.HistoryStore(tmp_path / "cache.pkl", tmp_path / "snapshots")
    assert again.history["runs"] == 3
    assert again.update()["runs"] == 3


def test_history_finds_the_first_run_at_any_minimum(tmp_path):
    history = dp.empty_history()
    low = game_handicap_rows(betclic_price="1.96")   # about +0.6%
    high = game_handicap_rows(betclic_price="2.30")  # about +12%
    dp.add_snapshot(history, "a", at(low, "2026-11-25T20:00:00Z"))
    dp.add_snapshot(history, "b", at(high, "2026-11-25T20:15:00Z"))
    now = datetime(2026, 11, 25, 21, 0, tzinfo=timezone.utc)

    def betclic(min_ev):
        return next(f for f in dp.history_flags(history, min_ev, now) if f["book"] == "Betclic")

    assert betclic(0.0)["captured_at"] == "2026-11-25T20:00:00Z"
    assert betclic(0.05)["captured_at"] == "2026-11-25T20:15:00Z"


def test_settled_flags_give_record_roi_and_units():
    flags = [
        {"event": "9", "market": "game_handicap", "side": "p1", "line": -3.5, "net": 2.0,
         "book": "Betclic", "started": True, "clv": 0.05, "ev": 0.03},
        {"event": "9", "market": "total_games", "side": "over", "line": 19.5, "net": 1.9,
         "book": "Betclic", "started": True, "clv": None, "ev": 0.04},
        {"event": "10", "market": "total_games", "side": "over", "line": 19.5, "net": 1.9,
         "book": "Betclic", "started": True, "clv": None, "ev": 0.02},
        {"event": "11", "market": "total_games", "side": "over", "line": 19.5, "net": 1.9,
         "book": "Betclic", "started": True, "clv": None, "ev": 0.02},
    ]
    outcomes = {"9": ("settled", [(6, 2), (6, 4)]), "10": ("void", "retired")}
    dp.settle_flags(flags, outcomes)

    assert [f["outcome"] for f in flags] == ["won", "lost", "void", None]
    stats = dp.history_stats(flags, "Betclic")
    assert (stats["won"], stats["lost"], stats["void"], stats["pending"]) == (1, 1, 1, 1)
    assert stats["units"] == pytest.approx(0.0)
    assert stats["roi"] == pytest.approx(0.0)


def kalshi(side, bid, ask):
    return {**BASE, "source": "kalshi", "book": "Kalshi", "market": "set1_winner",
            "side": side, "line": "", "price": "", "bid": bid, "ask": ask}


def test_market_outcomes_compare_exchanges_with_pinnacle_and_books():
    rows = game_handicap_rows() + [
        row("polymarket", "game_handicap", "p1", "-3.5", bid="0.46", ask="0.50"),
    ]
    found = {(o["side"]): o for o in dp.market_outcomes(rows, max_spread=0.04)}

    monnet = found["p2"]  # Monnet +3.5: Betclic 2.20, Polymarket 50-54
    quote = monnet["markets"]["Polymarket"]
    assert quote["mid"] == pytest.approx(0.52)
    assert quote["tight"]
    assert quote["ev"] == pytest.approx(2.20 * 0.52 - 1)
    assert quote["gap"] == pytest.approx(0.52 - monnet["pinnacle"])
    assert monnet["book"] == "Betclic" and monnet["line"] == 3.5
    # Sierra -3.5: the better book is Superbet, after its tax.
    assert found["p1"]["net"] == pytest.approx(2.30 * 0.88)


def test_kalshi_sides_are_scaled_and_wide_spreads_untrusted():
    rows = [kalshi("p1", "0.60", "0.70"), kalshi("p2", "0.36", "0.44"),
            row("pinnacle", "set1_winner", "p1", "", "1.60"),
            row("pinnacle", "set1_winner", "p2", "", "2.40")]
    found = {o["side"]: o for o in dp.market_outcomes(rows, max_spread=0.04)}
    p1, p2 = found["p1"]["markets"]["Kalshi"], found["p2"]["markets"]["Kalshi"]
    assert p1["mid"] + p2["mid"] == pytest.approx(1)
    assert not p1["tight"] and p1["spread"] == pytest.approx(0.10)

    stats = dp.agreement_stats(list(found.values()))
    assert stats["Kalshi"]["matches"] == 1 and stats["Kalshi"]["compared"] == 0


def test_market_record_greys_loose_quotes():
    rows = [kalshi("p1", "0.60", "0.70"), kalshi("p2", "0.36", "0.44"),
            row("betclic", "set1_winner", "p1", "", "1.80")]
    outcome = next(o for o in dp.market_outcomes(rows, 0.04) if o["side"] == "p1")
    record = dp.market_record(outcome, 0.02)
    assert record["Bet"] == "Sierra wins set 1"
    assert record["_style Kalshi"] == dp.GREY
    assert record["_style EV Kalshi"] == dp.GREY
    assert "Kalshi" in dp.table_html([record], dp.MARKETS_COLUMN_HELP)
