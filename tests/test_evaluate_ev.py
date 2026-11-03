"""Opportunity report (scripts/evaluate_ev.py). Local data only, no network."""

import sys

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import evaluate_ev as ev  # noqa: E402
import match_results as mr  # noqa: E402

NOW = datetime(2026, 11, 3, 20, 0, tzinfo=timezone.utc)
COVERED = date(2026, 11, 2)
START = "2026-11-03T18:00:00Z"


def session_row(hour, betclic=(1.5, 3.5), pinnacle=(1.6, 2.5), best_ev=0.05, **overrides):
    time = f"2026-11-03T{hour:02d}:00:00+00:00"
    row = {
        "pinnacle_player1": "Ann Lee",
        "pinnacle_player2": "Bea Kim",
        "pinnacle_tournament": "ITF Women Liberec - R1",
        "pinnacle_event_id": "7",
        "pinnacle_starts_at": START,
        "pinnacle_captured_at": time,
        "betclic_captured_at": time,
        "pinnacle_odds1": str(pinnacle[0]),
        "pinnacle_odds2": str(pinnacle[1]),
        "betclic_odds1": str(betclic[0]),
        "betclic_odds2": str(betclic[1]),
        "status": "FOUND",
        "confidence": "exact",
        "best_side": "2",
        "best_player": "Bea Kim",
        "best_ev": "" if best_ev is None else str(best_ev),
        "ev1_power": "-0.1",
        "ev2_power": "" if best_ev is None else str(best_ev),
        "probability2_power": "0.3",
    }
    for method, value in (("odds_ratio", 0.04), ("shin", 0.03), ("proportional", -0.01)):
        row[f"ev2_{method}"] = str(value)
    return {**row, **overrides}


def sighting(hour, odds=(1.6, 2.5), player1="Ann Lee", player2="Bea Kim"):
    return {
        "player1": player1,
        "player2": player2,
        "odds1": str(odds[0]),
        "odds2": str(odds[1]),
        "captured_at": f"2026-11-03T{hour:02d}:00:00+00:00",
        "starts_at": START,
    }


def by_day(winner="Bea Kim", loser="Ann Lee", result_type="completed"):
    return mr.index_results([{
        "date": "2026-11-03", "winner": winner, "loser": loser,
        "tournament": "ITF Liberec", "result_type": result_type,
    }])


def build(rows, sightings=(), days=None, now=NOW, min_ev=0.0, since=None):
    return ev.build_opportunity(rows, list(sightings), days or {}, COVERED, now, min_ev, since)


def test_row_measures_from_the_first_flagged_price():
    rows = [
        session_row(8, best_ev=None),
        session_row(9),
        session_row(10, best_ev=0.09, betclic=(1.5, 3.4)),
        session_row(11, best_ev=None, betclic=(1.5, 2.8)),
    ]

    item, late = build(rows, [sighting(10), sighting(11)])

    assert late is False
    assert item["bet_on"] == "Bea Kim"
    assert item["opponent"] == "Ann Lee"
    assert item["bet_price"] == 3.5
    assert item["first_ev"] == 0.05
    assert item["peak_ev"] == 0.09
    assert (item["runs_flagged"], item["runs_seen"]) == (2, 4)
    assert item["betclic_last"] == 2.8


def test_clv_uses_the_last_price_before_the_start_and_ignores_later_ones():
    rows = [session_row(9)]
    sightings = [sighting(10, (1.7, 2.3)), sighting(17, (1.8, 2.1)), sighting(19, (1.01, 30.0))]

    item, _ = build(rows, sightings)
    fair = mr_fair((1.8, 2.1), 1)

    assert item["pinnacle_last"] == 2.1
    assert item["clv"] == pytest.approx(3.5 * fair - 1)
    assert item["clv_state"] == "final"
    assert item["close_h"] == pytest.approx(1.0)


def mr_fair(odds, index):
    return ev.remove_margin(list(odds), ev.results_store.REFERENCE_METHOD)[index]


def test_clv_is_only_so_far_before_the_start():
    item, _ = build([session_row(9)], [sighting(10)], now=datetime(2026, 11, 3, 12, tzinfo=timezone.utc))

    assert item["clv_state"] == "so far"
    assert item["result"] == "pending"


def test_no_later_price_gives_no_clv():
    item, _ = build([session_row(9)], [sighting(9)])

    assert item["clv"] is None
    assert item["clv_state"] == "no later price"
    assert item["moved"] == "no later price"


def test_later_pinnacle_row_in_the_other_player_order_keeps_the_right_side():
    item, _ = build(
        [session_row(9)], [sighting(10, (2.1, 1.8), player1="Bea Kim", player2="Ann Lee")],
    )

    assert item["pinnacle_last"] == 2.1
    assert item["clv"] == pytest.approx(3.5 * mr_fair((2.1, 1.8), 0) - 1)


@pytest.mark.parametrize(
    ("betclic_later", "pinnacle_later", "expected"),
    [
        ((1.5, 2.8), (1.6, 2.5), "Betclic"),
        ((1.5, 3.5), (1.5, 2.9), "Pinnacle"),
        ((1.5, 2.8), (1.5, 2.9), "Betclic + Pinnacle"),
        ((1.5, 3.49), (1.6, 2.51), "neither"),
    ],
)
def test_who_moved(betclic_later, pinnacle_later, expected):
    rows = [session_row(9), session_row(10, betclic=betclic_later, pinnacle=pinnacle_later)]

    item, _ = build(rows, [sighting(10, pinnacle_later)])

    assert item["moved"] == expected


def test_methods_positive_counts_all_devig_methods():
    item, _ = build([session_row(9)], [])

    assert item["methods_positive"] == "3/4"
    assert item["ev_min"] == -0.01
    assert item["ev_max"] == 0.05


def test_win_and_loss_pay_from_the_first_price():
    won, _ = build([session_row(9)], [], by_day("Bea Kim", "Ann Lee"))
    lost, _ = build([session_row(9)], [], by_day("Ann Lee", "Bea Kim"))

    assert (won["result"], won["profit"]) == ("won", pytest.approx(2.5))
    assert (lost["result"], lost["profit"]) == ("lost", -1.0)


@pytest.mark.parametrize("result_type", ["retired", "walkover", "default"])
def test_incomplete_matches_are_shown_without_profit(result_type):
    item, _ = build([session_row(9)], [], by_day(result_type=result_type))

    assert item["result"] == f"won ({result_type})"
    assert item["profit"] is None


def test_missing_result_is_pending_until_the_covered_day_passes():
    item, _ = build([session_row(9)], [])
    assert item["result"] == "pending"

    old = ev.build_opportunity(
        [session_row(9)], [], {}, date(2026, 11, 10), NOW,
    )[0]
    assert old["result"] == "no result"


def test_flag_after_the_listed_start_is_left_out_and_counted():
    item, late = build([session_row(19)], [])

    assert item is None
    assert late is True


def test_not_found_or_negative_rows_are_not_opportunities():
    assert build([session_row(9, status="NOT FOUND")]) == (None, False)
    assert build([session_row(9, best_ev=-0.02)]) == (None, False)
    assert build([session_row(9, best_ev=None)]) == (None, False)


def test_filters():
    assert build([session_row(9, best_ev=0.01)], min_ev=0.02) == (None, False)
    assert build([session_row(9)], since=date(2026, 11, 4)) == (None, False)


def test_start_that_moved_later_uses_the_newest_one():
    rows = [session_row(19, pinnacle_starts_at="2026-11-03T18:00:00Z")]
    moved = {**sighting(20), "starts_at": "2026-11-03T22:00:00Z"}

    item, late = build(rows, [moved])

    assert late is False
    assert item["starts_at"] == "2026-11-03T22:00:00+00:00"


def test_build_all_sorts_oldest_first_and_counts_late_flags():
    later = [session_row(10, pinnacle_player1="Cy Oz", pinnacle_player2="Bea Kim")]
    earlier = [session_row(9)]
    too_late = [session_row(19, pinnacle_player1="Di Vo", pinnacle_player2="Bea Kim")]
    groups = {
        ev.match_key(r[0]["pinnacle_player1"], r[0]["pinnacle_player2"], r[0]["pinnacle_tournament"]): r
        for r in (later, earlier, too_late)
    }

    items, late = ev.build_all(groups, {}, {}, COVERED, NOW)

    assert [o["opponent"] for o in items] == ["Ann Lee", "Cy Oz"]
    assert late == 1


def test_match_key_ignores_player_order_but_not_the_round():
    assert ev.match_key("A B", "C D", "X - R1") == ev.match_key("C D", "A B", "X - R1")
    assert ev.match_key("A B", "C D", "X - R1") != ev.match_key("A B", "C D", "X - R2")


def test_csv_has_every_field(tmp_path):
    item, _ = build([session_row(9)], [])
    path = tmp_path / "out" / "opportunities.csv"

    assert set(item) == set(ev.FIELDS)

    ev.write_csv([item], path)

    assert ev.read_csv(path)[0]["bet_on"] == "Bea Kim"


def test_output_prints_with_missing_values(capsys):
    item, _ = build([session_row(9)], [])

    ev.print_table([item])
    ev.print_summary([item], too_late=2)

    out = capsys.readouterr().out
    assert "Bea Kim" in out
    assert "flagged only after the listed start: 2" in out
