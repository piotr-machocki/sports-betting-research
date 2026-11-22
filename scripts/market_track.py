"""
Statistics (all runs) for the Prediction markets tab: book prices flagged against
Polymarket, Kalshi and their average, judged like the Tennis page's track record.

Since 22 Nov 2026 (user's request). Logged and shown only: nothing here is sent to
Telegram or counts on the Tennis page. A signal is a Betclic or Superbet price whose
EV against a market's probability (middle of its bid and ask, spread within the
sidebar's limit) reaches the minimum EV; each match counts once, at its first such
flag before its start, like dashboard.first_flags. Its CLV is still taken against
Pinnacle's closing line and its result from match_results, so the market signals are
measured on the same scale as Pinnacle's. Each flag also says whether Pinnacle gave
the same price EV >= the minimum at that moment (agrees).

The slow part, matching a run's rows to its market file (about 0.5 s a run on the
VM), is done once per run and kept with the run files' signatures (session_candidates);
dashboard.MarketTrackLoader keeps that cache on disk and works the card out in a thread.
"""

from __future__ import annotations

import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

from live_monitor import SESSIONS_DIR, latest_per_match, read_session_rows
from market_compare import (
    MARKETS_DIR,
    far_from_pinnacle,
    SOURCE_LABELS,
    SOURCES,
    compare_session,
    probability,
    read_snapshot,
)

# Both markets at once (user, 22 Nov 2026; it replaced their average, which only
# blended the two): a signal needs EV >= the minimum against each of them.
BOTH = "Both"
# Below this probability a signal is a long shot, which the sidebar can leave out:
# markets overprice them, and a cent on a 5% price is already a fifth of it.
LONGSHOT_BELOW = 0.15
# Card rows in order: each market alone, then both agreeing.
REFERENCES = [SOURCE_LABELS[source] for source in SOURCES] + [BOTH]


def market_sessions(markets_dir: Path = MARKETS_DIR) -> dict[str, Path]:
    """Every session run with a market file, oldest first (books-only runs left out)."""
    found = {path.name.split(".")[0]: path for path in markets_dir.glob("*/session_*.csv.gz")}
    return dict(sorted(found.items()))


def signature(*paths: Path) -> tuple | None:
    try:
        return tuple((stat.st_mtime_ns, stat.st_size) for stat in (p.stat() for p in paths))
    except OSError:
        return None


def session_candidates(
    session_id: str, market_file: Path, cache: dict, sessions_dir: Path | None = None
) -> list[dict]:
    """Per match and side not started: the best book price, Pinnacle's probability
    and each market's (probability, spread), when some market gives it EV >= 0 (the
    slider's lowest minimum). Read again only when either file changed."""
    csv = (sessions_dir or SESSIONS_DIR) / f"{session_id}.csv"
    held = cache.get(session_id)
    sign = signature(csv, market_file)

    if sign is None:
        return held[1] if held else []
    if held and held[0] == sign:
        return held[1]

    rows = read_session_rows(csv)
    if rows is None:
        return held[1] if held else []

    # Every spread kept (limit 1 = $1): the sidebar's limit is applied per request.
    matches = compare_session(latest_per_match(rows), read_snapshot(market_file), 1.0)
    out = []

    for match in matches:
        if match["started"]:
            continue
        row = match["row"]
        for side in (1, 2):
            part = match["sides"][side]
            price = part["price"]
            markets = {
                SOURCE_LABELS[source]: (probability(view, side), view["spread"])
                for source, view in match["markets"].items()
                if probability(view, side) is not None and view["spread"] is not None
            }
            if not price or not markets:
                continue
            if max(chance for chance, _ in markets.values()) * price - 1 < 0:
                continue
            out.append({
                "Bet on": row.get(f"pinnacle_player{side}", ""),
                "Opponent": row.get(f"pinnacle_player{3 - side}", ""),
                "Tournament": row.get("pinnacle_tournament", ""),
                "_price": price,
                "_book": part["book"],
                "_pinnacle": part["pinnacle"],
                "_markets": markets,
                "_starts_at": row.get("pinnacle_starts_at") or "",
                "_captured_at": row.get("pinnacle_captured_at") or row.get("betclic_captured_at") or "",
            })

    cache[session_id] = (sign, out)
    return out


def reference_probability(candidate: dict, reference: str, max_spread: float) -> float | None:
    """The market's probability within the spread limit and not far from Pinnacle's
    (market_compare.MAX_PINNACLE_GAP: a new market's opening 50/50 is not a price);
    Both needs every market trusted and takes the lower probability, so its EV is
    the smaller of the two and reaches the minimum only when both do."""
    tight = {
        name: chance
        for name, (chance, spread) in candidate["_markets"].items()
        if spread <= max_spread + 1e-9 and not far_from_pinnacle(chance, candidate["_pinnacle"])
    }
    if reference == BOTH:
        if len(tight) < len(SOURCES):
            return None
        return min(tight.values())
    return tight.get(reference)


def flags_for(
    candidates: list[dict],
    reference: str,
    min_ev: float,
    max_spread: float,
    longshots: bool = True,
) -> list[dict]:
    """Every candidate as a record with Best EV against this reference (None left
    out, and long shots unless longshots) and whether Pinnacle agreed, oldest run first."""
    out = []
    for candidate in candidates:
        chance = reference_probability(candidate, reference, max_spread)
        if chance is None or (not longshots and chance < LONGSHOT_BELOW):
            continue
        price = candidate["_price"]
        pinnacle = candidate["_pinnacle"]
        out.append({
            **candidate,
            "Best EV": price * chance - 1,
            "_agree": pinnacle is not None and price * pinnacle - 1 >= min_ev,
        })
    return out


def market_record(
    dash: Any,
    sessions: dict[str, Path],
    cache: dict,
    sources: dict,
    min_ev: float,
    max_spread: float,
    now: datetime,
    longshots: bool = True,
) -> dict[str, dict]:
    """Each reference with at least one signal -> dashboard.track_stats plus the
    agreement with Pinnacle. dash is the dashboard module (its first_flags,
    settled_outcomes and track_stats), passed in so this file does not import it."""
    candidates = [
        candidate
        for session_id, path in sessions.items()
        for candidate in session_candidates(session_id, path, cache)
    ]
    record = {}

    for reference in REFERENCES:
        flags = dash.first_flags(
            flags_for(candidates, reference, min_ev, max_spread, longshots),
            "", min_ev, sources["sightings"],
        )
        if not flags:
            continue
        memo = cache.setdefault(("market outcomes", reference), {})
        settled = dash.settled_outcomes(flags, sources, now, memo)
        stats = dash.track_stats(settled)
        stats.update(agreement_stats(settled))
        record[reference] = stats

    return record


def agreement_stats(flags: list[dict]) -> dict:
    """How many signals Pinnacle agreed with, and the mean CLV of each group: if the
    markets find value Pinnacle does not, the market-only CLV says whether it is real."""
    def mean_clv(group: list[dict]) -> float | None:
        values = [flag["CLV"] for flag in group if flag.get("CLV") is not None]
        return statistics.mean(values) if values else None

    agree = [flag for flag in flags if flag["_agree"]]
    alone = [flag for flag in flags if not flag["_agree"]]
    return {
        "agree": len(agree),
        "agree_clv": mean_clv(agree),
        "alone_clv": mean_clv(alone),
    }
