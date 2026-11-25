"""
Settle the set and games markets of scripts/tennis_props.py from final set scores.

Since 25 Nov 2026 (user's request). Set scores come from match_results.load_results:
Matchstat's results.csv (covers ATP, Challenger, ITF and WTA; about 96% of Pinnacle
matches link) plus data/match_results/extra/*.csv, where the private
src/betting/books/superbet_results.py (``--all-linked DAYS``) adds every linked match it
sees finished on Superbet; Matchstat's row wins when both have a match. On 24 Nov 2026
the two agreed on all 143 complete matches both had. PinnFeed has no results (its
`score` field is live only), so there is no third source.

Scores are read from the winner's view ("6-4 3-6 10-8"; tiebreak points in brackets
are dropped). A last set of 10+ games is a match tiebreak: one set, and one game won,
for totals and handicaps, as the books count it. Only completed matches settle;
retirements, walkovers and defaults are void here (books' rules for those differ by
market, so they are left out rather than guessed).

    settle(market, side, line, sets) -> "won" | "lost" | "push" | "void"

`sets` are (player 1 games, player 2 games) per set in Pinnacle's order, from
`match_sets`.
"""

from __future__ import annotations

import re
from typing import Any

import match_results

SET_SCORE = re.compile(r"^(\d+)-(\d+)(?:\(\d+\))?$")


def parse_sets(result: str) -> list[tuple[int, int]] | None:
    """[(winner games, loser games), ...] or None for anything else (ret., w/o)."""
    parts = (result or "").split()
    if not parts:
        return None
    sets = []
    for part in parts:
        found = SET_SCORE.match(part)
        if not found:
            return None
        sets.append((int(found.group(1)), int(found.group(2))))
    return sets


def match_sets(link: dict[str, Any]) -> list[tuple[int, int]] | None:
    """Set scores in Pinnacle's order for a linked, completed match, else None."""
    if not link.get("linked"):
        return None
    row = link["result"]
    if row.get("result_type") != "completed":
        return None
    sets = parse_sets(row.get("result", ""))
    if not sets:
        return None
    won = sum(a > b for a, b in sets)
    if won <= len(sets) - won:
        return None  # not from the winner's view: do not guess
    return sets if link["winner_side"] == 1 else [(b, a) for a, b in sets]


def games(one_set: tuple[int, int], last: bool) -> tuple[int, int]:
    """Games of a set; a match tiebreak (last set, 10+) counts as one game."""
    a, b = one_set
    if last and max(a, b) >= 10:
        return (1, 0) if a > b else (0, 1)
    return a, b


def over_under(total: float, line: float, side: str) -> str:
    if total == line:
        return "push"
    return "won" if (total > line) == (side == "over") else "lost"


def handicap(diff: float, line: float, side: str) -> str:
    """diff = player 1 minus player 2; `line` is the side's own handicap."""
    margin = (diff if side == "p1" else -diff) + line
    if margin == 0:
        return "push"
    return "won" if margin > 0 else "lost"


def settle(market: str, side: str, line: float | None, sets: list[tuple[int, int]] | None) -> str:
    if not sets:
        return "void"
    counted = [games(s, i == len(sets) - 1) for i, s in enumerate(sets)]
    p1_sets = sum(a > b for a, b in sets)
    p2_sets = len(sets) - p1_sets
    p1_games = sum(a for a, _ in counted)
    p2_games = sum(b for _, b in counted)

    def winner_is(want: str, a: int, b: int) -> str:
        return "won" if (a > b) == (want == "p1") else "lost"

    if market == "match_winner":
        return winner_is(side, p1_sets, p2_sets)
    if market in ("set1_winner", "set2_winner"):
        index = int(market[3]) - 1
        if len(sets) <= index:
            return "void"
        return winner_is(side, *sets[index])
    if market == "set_score":
        return "won" if side == f"{p1_sets}-{p2_sets}" else "lost"
    if line is None:
        return "void"
    if market == "set_handicap":
        return handicap(p1_sets - p2_sets, line, side)
    if market == "total_sets":
        return over_under(len(sets), line, side)
    if market == "game_handicap":
        return handicap(p1_games - p2_games, line, side)
    if market == "total_games":
        return over_under(p1_games + p2_games, line, side)
    if market in ("set1_game_handicap", "set1_total_games", "set2_total_games"):
        index = 1 if market.startswith("set2") else 0
        if len(sets) <= index:
            return "void"
        a, b = counted[index]
        if market.endswith("handicap"):
            return handicap(a - b, line, side)
        return over_under(a + b, line, side)
    if market in ("p1_games", "p2_games"):
        return over_under(p1_games if market == "p1_games" else p2_games, line, side)
    return "void"


def profit(outcome: str, net_price: float) -> float | None:
    """Units won on a 1-unit stake at the price actually paid (Superbet after tax)."""
    return {"won": net_price - 1, "lost": -1.0, "push": 0.0}.get(outcome)


class Results:
    """match_results' rows indexed once, for linking many matches."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.by_day = match_results.index_results(
            rows if rows is not None else match_results.load_results())

    def sets(self, player1: str, player2: str, starts_at: str, tournament: str = "") -> tuple[str, Any]:
        """("settled", sets) | ("void", reason) | ("pending", reason)."""
        link = match_results.link_match(player1, player2, self.by_day,
                                        starts_at=starts_at, tournament=tournament)
        if not link.get("linked"):
            return "pending", link.get("reason", "no result")
        sets = match_sets(link)
        if sets is None:
            return "void", link["result"].get("result_type") or "unreadable score"
        return "settled", sets
