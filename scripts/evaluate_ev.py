"""List every positive-EV opportunity found so far and test whether it was real.

One row per match, measured from the first session that flagged it: the price
you could have bet, how both books moved afterwards, closing line value (CLV),
whether all devig methods agree, and the result. Reads local files only, so it
makes no API requests:
    data/results/raw/session_*.csv  - flagged prices and later Betclic prices
    data/pinnacle/raw/*.csv         - later Pinnacle prices
    data/match_results/results.csv  - finished matches

CLV = bet price * Pinnacle's fair probability at the last price seen before the
start - 1. It is "final" once the match has started and "so far" before that.
The last price is only as close to the start as the last run, so `close_h` says
how many hours before the start it was seen. Profit is for a stake of 1 and
counts completed matches only: retirements and walkovers are shown but left out,
because how they settle depends on the bookmaker's rules.

Usage:
    python scripts/evaluate_ev.py                  # all opportunities
    python scripts/evaluate_ev.py --min-ev 0.02    # only EV of at least 2%
    python scripts/evaluate_ev.py --from 2026-11-03
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import statistics
import sys

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import match_results as mr  # noqa: E402
import results_store  # noqa: E402

from betting.margin import METHODS, remove_margin  # noqa: E402

OUTPUT_DIR = results_store.RESULTS_DIR / "ev_evaluation"
OUTPUT_CSV = OUTPUT_DIR / "opportunities.csv"

MOVE_THRESHOLD = 0.02  # a price change below 2% counts as not moved
CLOSE_HOURS = 3.0  # a last price this near the start is close to a closing price

FIELDS = [
    "first_flagged_at", "last_flagged_at", "runs_flagged", "runs_seen",
    "bet_on", "opponent", "tournament", "pinnacle_event_id", "starts_at", "match_quality",
    "first_ev", "peak_ev", "last_ev",
    "bet_price", "betclic_last", "betclic_move",
    "pinnacle_at_flag", "pinnacle_last", "pinnacle_move", "moved",
    "clv", "clv_state", "close_h",
    "methods_positive", "ev_min", "ev_max",
    "fair_probability", "result", "result_type", "profit", "url",
]


def number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def match_key(player1: str, player2: str, tournament: str | None) -> tuple:
    """Either player order; the tournament text includes the round, so the same
    pair meeting again is a different key."""
    return frozenset((mr.name_tokens(player1), mr.name_tokens(player2))), tournament


def session_time(row: dict[str, str]) -> datetime | None:
    return parse_time(row.get("betclic_captured_at")) or parse_time(row.get("pinnacle_captured_at"))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def load_observations() -> dict[tuple, list[dict[str, str]]]:
    """Every session row with a time, by match, oldest first."""
    groups: dict[tuple, list[dict[str, str]]] = {}

    for path in sorted(results_store.RAW_DIR.glob("session_*.csv")):
        for row in read_csv(path):
            if session_time(row) and row.get("pinnacle_player1") and row.get("pinnacle_player2"):
                key = match_key(row["pinnacle_player1"], row["pinnacle_player2"], row.get("pinnacle_tournament"))
                groups.setdefault(key, []).append(row)

    for rows in groups.values():
        rows.sort(key=session_time)

    return groups


def load_sightings() -> dict[tuple, list[dict[str, str]]]:
    """Every Pinnacle snapshot row by match, oldest first. A session's request is
    stored in two files, so the same capture is kept once."""
    groups: dict[tuple, dict[str, dict[str, str]]] = {}

    for path in sorted(mr.PINNACLE_RAW_DIR.glob("*.csv")):
        for row in read_csv(path):
            if parse_time(row.get("captured_at")) and row.get("player1") and row.get("player2"):
                key = match_key(row["player1"], row["player2"], row.get("tournament"))
                groups.setdefault(key, {})[row["captured_at"]] = row

    return {
        key: sorted(rows.values(), key=lambda r: parse_time(r["captured_at"]))
        for key, rows in groups.items()
    }


def latest_start(rows: list[dict[str, str]], sightings: list[dict[str, str]]) -> datetime | None:
    """The start can move: the most recently seen one wins."""
    seen = [
        (session_time(r), parse_time(r.get("pinnacle_starts_at"))) for r in rows
    ] + [
        (parse_time(s["captured_at"]), parse_time(s.get("starts_at"))) for s in sightings
    ]
    known = [pair for pair in seen if pair[1]]
    return max(known, key=lambda pair: pair[0])[1] if known else None


def is_flagged(row: dict[str, str], min_ev: float) -> bool:
    ev = number(row.get("best_ev"))
    return (
        ev is not None and ev > 0 and ev >= min_ev
        and row.get("status") == "FOUND" and bool(row.get("best_player"))
    )


def side_of(row_player1: str, row_player2: str, tokens: frozenset) -> int | None:
    if mr.name_tokens(row_player1) == tokens:
        return 1
    if mr.name_tokens(row_player2) == tokens:
        return 2
    return None


def move(first: float | None, last: float | None) -> float | None:
    return last / first - 1 if first and last else None


def who_moved(betclic: float | None, pinnacle: float | None) -> str:
    if betclic is None and pinnacle is None:
        return "no later price"
    moved = [
        name
        for name, change in (("Betclic", betclic), ("Pinnacle", pinnacle))
        if change is not None and abs(change) >= MOVE_THRESHOLD
    ]
    return " + ".join(moved) if moved else "neither"


def settle(
    flag: dict[str, str],
    side: int,
    price: float,
    start: datetime | None,
    now: datetime,
    by_day: dict[str, list[dict[str, Any]]],
    covered: date | None,
) -> tuple[str, str, float | None]:
    """(result, result_type, profit). Pending until the match can have been played."""
    if start and start > now:
        return "pending", "", None

    reference = start.isoformat() if start else None
    captured = flag.get("pinnacle_captured_at") or flag.get("betclic_captured_at")

    link = mr.link_match(
        flag["pinnacle_player1"], flag["pinnacle_player2"], by_day,
        starts_at=reference, captured_at=captured, tournament=flag.get("pinnacle_tournament"),
    )

    if not link["linked"]:
        day = str(reference or captured)[:10]
        waiting = covered is None or day >= covered.isoformat()
        return ("pending" if waiting else "no result"), "", None

    result_type = link["result"].get("result_type") or ""
    won = link["winner_side"] == side

    if result_type != "completed":
        return f"{'won' if won else 'lost'} ({result_type})", result_type, None

    return ("won" if won else "lost"), result_type, price - 1 if won else -1.0


def build_opportunity(
    rows: list[dict[str, str]],
    sightings: list[dict[str, str]],
    by_day: dict[str, list[dict[str, Any]]],
    covered: date | None,
    now: datetime,
    min_ev: float = 0.0,
    since: date | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """One match's report row, or None. The second value is True when the match
    was flagged only after its listed start, so nobody could have bet it."""
    start = latest_start(rows, sightings)

    def before_start(when: datetime | None) -> bool:
        return when is not None and (start is None or when < start)

    candidates = [r for r in rows if is_flagged(r, min_ev)]
    flags = [r for r in candidates if before_start(session_time(r))]

    if not flags:
        return None, bool(candidates)

    first = flags[0]
    flagged_at = session_time(first)

    if since and flagged_at.date() < since:
        return None, False

    tokens = mr.name_tokens(first["best_player"])
    side = side_of(first["pinnacle_player1"], first["pinnacle_player2"], tokens)
    price = number(first.get(f"betclic_odds{side}")) if side else None

    if not price:
        return None, False

    # Betclic after the flag: rows priced before the start, in this player's order.
    later_prices, last_ev = [], None

    for row in rows:
        if session_time(row) < flagged_at or not before_start(session_time(row)):
            continue

        row_side = side_of(row["pinnacle_player1"], row["pinnacle_player2"], tokens)
        row_price = number(row.get(f"betclic_odds{row_side}")) if row_side else None

        if row_price and row.get("status") == "FOUND":
            later_prices.append(row_price)
            last_ev = number(row.get(results_store.raw_ev_column(results_store.REFERENCE_METHOD, row_side)))

    # Pinnacle after the flag; the flag's own capture is not a later price.
    flag_pinnacle_at = parse_time(first.get("pinnacle_captured_at"))
    later_pinnacle = [
        s for s in sightings
        if flag_pinnacle_at and parse_time(s["captured_at"]) > flag_pinnacle_at
        and before_start(parse_time(s["captured_at"]))
    ]

    pinnacle_at_flag = number(first.get(f"pinnacle_odds{side}"))
    pinnacle_last = clv = close_h = None
    clv_state = "no later price"

    for sighting in reversed(later_pinnacle):
        odds = [number(sighting.get("odds1")), number(sighting.get("odds2"))]
        s_side = side_of(sighting["player1"], sighting["player2"], tokens)

        if not s_side or not all(o and o > 1 for o in odds):
            continue

        pinnacle_last = odds[s_side - 1]
        fair = remove_margin(odds, results_store.REFERENCE_METHOD)
        clv = price * fair[s_side - 1] - 1

        if start:
            close_h = (start - parse_time(sighting["captured_at"])).total_seconds() / 3600
            clv_state = "final" if start <= now else "so far"
        else:
            clv_state = "start unknown"
        break

    betclic_last = later_prices[-1] if len(later_prices) > 1 else None
    betclic_move = move(price, betclic_last)
    pinnacle_move = move(pinnacle_at_flag, pinnacle_last)

    evs = [
        ev
        for ev in (number(first.get(results_store.raw_ev_column(m, side))) for m in METHODS)
        if ev is not None
    ]
    result, result_type, profit = settle(first, side, price, start, now, by_day, covered)
    fair_probability = number(first.get(results_store.raw_probability_column(results_store.REFERENCE_METHOD, side)))

    opportunity = {
        "first_flagged_at": flagged_at.isoformat(),
        "last_flagged_at": session_time(flags[-1]).isoformat(),
        "runs_flagged": len(flags),
        "runs_seen": len(rows),
        "bet_on": first["best_player"],
        "opponent": first[f"pinnacle_player{3 - side}"],
        "tournament": first.get("pinnacle_tournament") or "",
        "pinnacle_event_id": first.get("pinnacle_event_id") or "",
        "starts_at": start.isoformat() if start else "",
        "match_quality": first.get("confidence") or "",
        "first_ev": number(first["best_ev"]),
        "peak_ev": max(number(r["best_ev"]) for r in flags),
        "last_ev": last_ev,
        "bet_price": price,
        "betclic_last": betclic_last,
        "betclic_move": betclic_move,
        "pinnacle_at_flag": pinnacle_at_flag,
        "pinnacle_last": pinnacle_last,
        "pinnacle_move": pinnacle_move,
        "moved": who_moved(betclic_move, pinnacle_move),
        "clv": clv,
        "clv_state": clv_state,
        "close_h": close_h,
        "methods_positive": f"{sum(ev > 0 for ev in evs)}/{len(METHODS)}",
        "ev_min": min(evs) if evs else None,
        "ev_max": max(evs) if evs else None,
        "fair_probability": fair_probability,
        "result": result,
        "result_type": result_type,
        "profit": profit,
        "url": first.get("url") or "",
    }
    return opportunity, False


def build_all(
    observations: dict[tuple, list[dict[str, str]]],
    sightings: dict[tuple, list[dict[str, str]]],
    by_day: dict[str, list[dict[str, Any]]],
    covered: date | None,
    now: datetime,
    min_ev: float = 0.0,
    since: date | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """All opportunities, oldest first, and how many were flagged only after the start."""
    opportunities, too_late = [], 0

    for key, rows in observations.items():
        item, late = build_opportunity(rows, sightings.get(key, []), by_day, covered, now, min_ev, since)
        too_late += late

        if item:
            opportunities.append(item)

    opportunities.sort(key=lambda o: o["first_flagged_at"])
    return opportunities, too_late


# ---------- output ----------

def move_text(item: dict[str, Any]) -> str:
    """Who moved and which way: a lower Betclic price means it cut the bet, a
    higher Pinnacle price means the fair price moved against it."""
    parts = [
        f"{name} {change:+.0%}"
        for name, change in (("Betclic", item["betclic_move"]), ("Pin", item["pinnacle_move"]))
        if change is not None and abs(change) >= MOVE_THRESHOLD
    ]
    return ", ".join(parts) or item["moved"]


def percent(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value:+.{digits}%}"


def odds_text(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def local_time(value: str) -> str:
    parsed = parse_time(value)
    return parsed.astimezone().strftime("%d %b %H:%M") if parsed else "-"


def clv_summary(items: list[dict[str, Any]]) -> str:
    if not items:
        return "n=0"
    values = [o["clv"] for o in items]
    return (
        f"n={len(values)}, mean {percent(statistics.mean(values))}, "
        f"median {percent(statistics.median(values))}, "
        f"{sum(v > 0 for v in values) / len(values):.0%} positive"
    )


def profit_summary(items: list[dict[str, Any]]) -> str:
    settled = [o for o in items if o["profit"] is not None]

    if not settled:
        return "n=0"

    profits = [o["profit"] for o in settled]
    wins = sum(p > 0 for p in profits)
    expected = sum(o["fair_probability"] or 0 for o in settled)
    margin = 1.96 * statistics.pstdev(profits) / math.sqrt(len(profits))

    return (
        f"n={len(settled)}, won {wins} (expected {expected:.1f}), "
        f"ROI {percent(sum(profits) / len(profits))} +/- {margin:.1%}, "
        f"EV claimed {percent(statistics.mean(o['first_ev'] for o in settled))}"
    )


def print_group(label: str, items: list[dict[str, Any]]) -> None:
    final = [o for o in items if o["clv_state"] == "final" and o["clv"] is not None]
    near = [o for o in final if o["close_h"] is not None and o["close_h"] <= CLOSE_HOURS]
    so_far = [o for o in items if o["clv_state"] != "final" and o["clv"] is not None]

    print(f"{label}: {len(items)} matches")
    print(f"  CLV final:                {clv_summary(final)}")
    print(f"  CLV final, within {CLOSE_HOURS:.0f} h:    {clv_summary(near)}")
    print(f"  CLV so far (not started): {clv_summary(so_far)}")
    print(f"  Result, stake 1:          {profit_summary(items)}")


def print_summary(items: list[dict[str, Any]], too_late: int) -> None:
    print("\n========== IS THE EV REAL? ==========")
    print_group("All opportunities", items)
    print_group("Positive under all 4 methods", [
        o for o in items if o["methods_positive"] == f"{len(METHODS)}/{len(METHODS)}"
    ])
    print_group("First EV of at least 2%", [o for o in items if o["first_ev"] >= 0.02])

    moved = Counter(o["moved"] for o in items)
    alive = sum(o["last_ev"] is not None and o["last_ev"] > 0 for o in items)
    results = Counter(o["result"] for o in items)

    print(f"\nWho moved after the flag: {dict(moved)}")
    print(f"Still positive at the last sighting: {alive} of {len(items)}")
    print(f"Results: {dict(results)}")

    if too_late:
        print(f"Left out, flagged only after the listed start: {too_late}")


def print_table(items: list[dict[str, Any]]) -> None:
    print("\n========== OPPORTUNITIES ==========")
    print(
        "EV: first -> last sighting | Runs: flagged/seen | Methods: devig methods with EV > 0 | "
        "Close: hours before the start of the last price\n"
        "Moved: a lower Betclic price is a cut bet; a higher Pinnacle price moved against the bet\n"
    )
    header = (
        f"{'Flagged':<13}{'Bet on':<25}{'Tournament':<27}{'EV':<15}{'Runs':<7}"
        f"{'Betclic':<14}{'Pinnacle':<14}{'Moved':<24}{'CLV':<8}{'Close':<7}{'Methods':<8}"
        f"{'Starts':<13}{'Result':<17}{'Profit':<7}"
    )
    print(header)
    print("-" * len(header))

    for o in items:
        betclic = f"{odds_text(o['bet_price'])}->{odds_text(o['betclic_last'])}"
        pinnacle = f"{odds_text(o['pinnacle_at_flag'])}->{odds_text(o['pinnacle_last'])}"
        ev = f"{percent(o['first_ev'])}->{percent(o['last_ev'])}"
        clv = percent(o["clv"]) + ("" if o["clv_state"] == "final" else "~")
        close = "-" if o["close_h"] is None else f"{o['close_h']:.1f}h"
        profit = "-" if o["profit"] is None else f"{o['profit']:+.2f}"

        print(
            f"{local_time(o['first_flagged_at']):<13}{o['bet_on'][:24]:<25}{o['tournament'][:26]:<27}"
            f"{ev:<15}{str(o['runs_flagged']) + '/' + str(o['runs_seen']):<7}"
            f"{betclic:<14}{pinnacle:<14}{move_text(o):<24}{clv:<8}{close:<7}{o['methods_positive']:<8}"
            f"{local_time(o['starts_at']):<13}{o['result'][:16]:<17}{profit:<7}"
        )

    print("\n~ = not final: the match has not started, or its start is unknown (older rows).")


def write_csv(items: list[dict[str, Any]], path: Path = OUTPUT_CSV) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(items)
    path.parent.mkdir(parents=True, exist_ok=True)
    mr.write_atomic(path, buffer.getvalue())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--min-ev", type=float, default=0.0,
                        help="only matches flagged with at least this EV (0.02 = 2%%)")
    parser.add_argument("--from", dest="since", type=date.fromisoformat, default=None,
                        help="only matches first flagged on or after this day (YYYY-MM-DD)")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    results = mr.load_results() if mr.RESULTS_CSV.exists() else []
    covered = mr.read_covered_through()

    items, too_late = build_all(
        load_observations(), load_sightings(), mr.index_results(results), covered,
        datetime.now(timezone.utc), args.min_ev, args.since,
    )

    if not items:
        print("No positive-EV opportunities found.")
        return

    print_table(items)
    print_summary(items, too_late)

    if covered:
        print(f"\nResults covered through {covered}; the newest days can still be incomplete.")

    try:
        write_csv(items)
        print(f"All columns: {OUTPUT_CSV}")
    except OSError as exc:
        print(f"WARNING: could not write {OUTPUT_CSV}: {exc}")


if __name__ == "__main__":
    main()
