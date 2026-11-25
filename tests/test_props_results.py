"""Settling tennis set and games markets from final set scores (scripts/props_results.py)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import props_results as pr  # noqa: E402

# Player 1 wins 6-4 3-6 7-6: 16-16 games, 2-1 sets.
SETS = [(6, 4), (3, 6), (7, 6)]


@pytest.mark.parametrize("market, side, line, outcome", [
    ("match_winner", "p1", None, "won"),
    ("match_winner", "p2", None, "lost"),
    ("set1_winner", "p1", None, "won"),
    ("set2_winner", "p1", None, "lost"),
    ("set_score", "2-1", None, "won"),
    ("set_score", "2-0", None, "lost"),
    ("set_handicap", "p1", -1.5, "lost"),
    ("set_handicap", "p2", 1.5, "won"),
    ("total_sets", "over", 2.5, "won"),
    ("game_handicap", "p1", -0.5, "lost"),
    ("game_handicap", "p2", 2.5, "won"),
    ("game_handicap", "p1", 0, "push"),
    ("total_games", "over", 31.5, "won"),
    ("total_games", "under", 32, "push"),
    ("set1_total_games", "under", 9.5, "lost"),
    ("set1_game_handicap", "p2", 2.5, "won"),
    ("set1_game_handicap", "p2", 1.5, "lost"),
    ("set2_total_games", "over", 8.5, "won"),
    ("p1_games", "over", 15.5, "won"),
    ("p2_games", "under", 15.5, "lost"),
])
def test_every_market(market, side, line, outcome):
    assert pr.settle(market, side, line, SETS) == outcome


def test_match_tiebreak_is_one_set_and_one_game():
    sets = [(6, 3), (3, 6), (10, 8)]  # 9-9 in the sets, the tiebreak one game for p1
    assert pr.settle("total_games", "over", 18.5, sets) == "won"   # 19 games
    assert pr.settle("game_handicap", "p1", -1.5, sets) == "lost"  # 10-9 in games
    assert pr.settle("game_handicap", "p1", -0.5, sets) == "won"
    assert pr.settle("total_sets", "over", 2.5, sets) == "won"
    assert pr.settle("match_winner", "p1", None, sets) == "won"


def test_second_set_markets_are_void_without_a_second_set():
    assert pr.settle("set2_winner", "p1", None, [(6, 0)]) == "void"
    assert pr.settle("total_games", "over", 10.5, None) == "void"


def test_parse_and_orient_scores():
    assert pr.parse_sets("7-6(5) 6-7(8) 6-3") == [(7, 6), (6, 7), (6, 3)]
    assert pr.parse_sets("6-3 1-0 ret.") is None
    assert pr.parse_sets("") is None

    row = {"result": "6-4 6-3", "result_type": "completed"}
    assert pr.match_sets({"linked": True, "winner_side": 2, "result": row}) == [(4, 6), (3, 6)]
    assert pr.match_sets({"linked": True, "winner_side": 1, "result": {**row, "result_type": "retired"}}) is None
    assert pr.match_sets({"linked": False}) is None


def test_profit_at_the_price_paid():
    assert pr.profit("won", 1.9) == pytest.approx(0.9)
    assert pr.profit("lost", 1.9) == -1
    assert pr.profit("push", 1.9) == 0
    assert pr.profit("void", 1.9) is None


def test_results_link_and_orient(tmp_path):
    rows = [{"match_id": "1", "tour": "atp", "date": "2026-11-24", "tournament": "ITF Men Monastir",
             "round_id": "", "winner": "Ivan Biletic", "loser": "Valentin Guadagno",
             "result": "6-2 7-5", "result_type": "completed", "is_doubles": "False",
             "odds_winner": "", "odds_loser": ""}]
    results = pr.Results(rows)
    assert results.sets("Valentin Guadagno", "Ivan Biletic", "2026-11-24T10:00:00Z") == (
        "settled", [(2, 6), (5, 7)])
    assert results.sets("Someone Else", "Another One", "2026-11-24T10:00:00Z")[0] == "pending"
