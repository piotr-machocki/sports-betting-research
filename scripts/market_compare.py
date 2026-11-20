"""
Prediction-market prices (scripts/tennis_markets.py) set against one session's matches.

Since 20 Nov 2026 (user's request). Comparison only: nothing here sends alerts or feeds
the Tennis page, Telegram or the Track record. It answers two questions for the
dashboard's Prediction markets subtab: how close are Polymarket and Kalshi to Pinnacle's devigged
probability, and what would the Betclic and Superbet prices be worth if a market's
probability were the true one.

A market's probability is the middle of its bid and ask: there is no margin to take
out, the spread is the uncertainty. Kalshi has a book per player, so its two middles
are scaled to add up to 1. A row whose wider side's spread is above the chosen limit
is "wide": shown, but its EV is not trusted (a thin market can sit on a stale quote).

Matching is by names only, never by surname alone: both players (every partner in
doubles) must match, in either order, and a shared surname also needs matching
initials when both names have more words (same-surname players do meet). Polymarket
must start within MAX_START_GAP_HOURS of Pinnacle; Kalshi gives no start time (the
date in its ticker is when the market opened), so it has no time check.
"""

from __future__ import annotations

import csv
import gzip
import re
import statistics
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from results_store import BETCLIC_MIN_ODDS  # noqa: E402
MARKETS_DIR = ROOT / "data" / "markets" / "tennis"

SOURCES = ("polymarket", "kalshi")
SOURCE_LABELS = {"polymarket": "Polymarket", "kalshi": "Kalshi"}
MAX_START_GAP_HOURS = 24
DEFAULT_MAX_SPREAD = 0.04  # 4 cents on the wider side
SUPERBET_TAX_FACTOR = 0.88


def number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def parse_time(text: Any) -> datetime | None:
    if not text:
        return None
    try:
        value = datetime.fromisoformat(str(text).replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ------------------------------------------------------------------- files --


def snapshot_path(session_id: str, markets_dir: Path = MARKETS_DIR) -> Path | None:
    """The market file of a run. It is written after the run, under that moment's UTC
    date, so a run just before midnight lands in the next day's folder."""
    found = sorted(markets_dir.glob(f"*/{session_id}.csv.gz"))
    return found[-1] if found else None


def latest_snapshots(markets_dir: Path = MARKETS_DIR, days: int = 2) -> list[Path]:
    """Market files of the newest days, newest first."""
    folders = sorted((p for p in markets_dir.glob("*") if p.is_dir()), reverse=True)[:days]
    files = [path for folder in folders for path in folder.glob("session_*.csv.gz")]
    return sorted(files, key=lambda path: path.name, reverse=True)


def read_snapshot(path: Path) -> list[dict]:
    try:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as file:
            return list(csv.DictReader(file))
    except (OSError, EOFError, csv.Error):
        return []


# ---------------------------------------------------------------- matching --


def words(name: str) -> list[str]:
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.findall(r"[a-z]+", plain.lower())


def same_person(first: str, second: str) -> bool:
    """A shared surname (either name's last word in the other, which also covers
    reversed name order and extra middle names), or a last word of 4+ letters inside
    the other name glued together. When both names have words besides that surname,
    one initial must be shared too."""
    a, b = words(first), words(second)
    if not a or not b:
        return False

    if a[-1] in b:
        surname = a[-1]
    elif b[-1] in a:
        surname = b[-1]
    elif len(a[-1]) >= 4 and a[-1] in "".join(b):
        return True
    elif len(b[-1]) >= 4 and b[-1] in "".join(a):
        return True
    else:
        return False

    rest_a = {word[0] for word in a if word != surname}
    rest_b = {word[0] for word in b if word != surname}
    return not rest_a or not rest_b or bool(rest_a & rest_b)


def partners(name: str) -> list[str]:
    return [part.strip() for part in name.split("/") if part.strip()]


def same_team(first: str, second: str) -> bool:
    """One player, or a doubles pair in either partner order."""
    a, b = partners(first), partners(second)
    if len(a) != len(b) or not a:
        return False
    if len(a) == 1:
        return same_person(a[0], b[0])
    return (same_person(a[0], b[0]) and same_person(a[1], b[1])) or (
        same_person(a[0], b[1]) and same_person(a[1], b[0])
    )


def orientation(pinnacle: tuple[str, str], market: dict) -> int | None:
    """1 if the market lists Pinnacle's player 1 first, 2 if reversed, None if no match."""
    p1, p2 = pinnacle
    m1, m2 = market.get("player1") or "", market.get("player2") or ""
    if same_team(p1, m1) and same_team(p2, m2):
        return 1
    if same_team(p1, m2) and same_team(p2, m1):
        return 2
    return None


def find_market(row: dict, markets: list[dict]) -> tuple[dict, int] | None:
    """The market row of one source for a Pinnacle row: names in either order, then
    for Polymarket a start within MAX_START_GAP_HOURS, the closest one winning."""
    pinnacle = (row.get("pinnacle_player1") or "", row.get("pinnacle_player2") or "")
    starts = parse_time(row.get("pinnacle_starts_at"))
    best: tuple[float, dict, int] | None = None

    for market in markets:
        order = orientation(pinnacle, market)
        if order is None:
            continue
        market_start = parse_time(market.get("starts_at"))
        if starts and market_start:
            gap = abs((market_start - starts).total_seconds())
            if gap > MAX_START_GAP_HOURS * 3600:
                continue
        else:
            gap = 0.0
        if best is None or gap < best[0]:
            best = (gap, market, order)

    return (best[1], best[2]) if best else None


# ----------------------------------------------------------------- prices --


def side_quote(market: dict, side: int) -> tuple[float | None, float | None]:
    bid, ask = number(market.get(f"bid{side}")), number(market.get(f"ask{side}"))
    return bid, ask


def market_view(market: dict, order: int) -> dict:
    """Bid, ask, middle and spread per side in Pinnacle's player order, and the
    probability for player 1. One side without a quote takes 1 - the other."""
    sides = {}
    for pin_side in (1, 2):
        market_side = pin_side if order == 1 else 3 - pin_side
        bid, ask = side_quote(market, market_side)
        mid = (bid + ask) / 2 if bid is not None and ask is not None else None
        spread = ask - bid if bid is not None and ask is not None else None
        sides[pin_side] = {"bid": bid, "ask": ask, "mid": mid, "spread": spread}

    mid1, mid2 = sides[1]["mid"], sides[2]["mid"]
    if mid1 is not None and mid2 is not None and mid1 + mid2 > 0:
        probability1 = mid1 / (mid1 + mid2)
    elif mid1 is not None:
        probability1 = mid1
    elif mid2 is not None:
        probability1 = 1 - mid2
    else:
        probability1 = None

    spreads = [side["spread"] for side in sides.values() if side["spread"] is not None]
    return {
        "sides": sides,
        "probability1": probability1,
        "spread": max(spreads) if spreads else None,
        "volume": number(market.get("volume")),
        "liquidity": number(market.get("liquidity")),
        "event_id": market.get("event_id") or "",
        "starts_at": parse_time(market.get("starts_at")),
        "names": (market.get("player1") or "", market.get("player2") or "")
        if order == 1 else (market.get("player2") or "", market.get("player1") or ""),
    }


def probability(view: dict | None, side: int) -> float | None:
    if not view or view["probability1"] is None:
        return None
    return view["probability1"] if side == 1 else 1 - view["probability1"]


def book_prices(row: dict, side: int) -> dict[str, float]:
    """Net decimal prices per book for one side: Betclic as shown (tax included,
    nothing below its 1.14 minimum), only where the worker priced the row (FOUND, odds
    complete, not live); Superbet FOUND rows after its 12% tax."""
    prices = {}
    betclic = number(row.get(f"betclic_odds_for_pin_p{side}"))
    if number(row.get(f"ev{side}")) is not None and betclic and betclic >= BETCLIC_MIN_ODDS:
        prices["Betclic"] = betclic

    superbet = number(row.get(f"superbet_price{side}"))
    if row.get("superbet_status") == "FOUND" and superbet:
        prices["Superbet"] = superbet * SUPERBET_TAX_FACTOR
    return prices


def power_devig(odds1: float, odds2: float) -> tuple[float, float]:
    """The power method (results_store.REFERENCE_METHOD) for two prices: p = q ** k
    with k such that both add up to 1. Bisection here, since margin.brentq imports
    scipy, which costs the dashboard seconds on a cold start."""
    q1, q2 = 1 / odds1, 1 / odds2
    low, high = 0.01, 50.0
    for _ in range(100):
        k = (low + high) / 2
        if q1 ** k + q2 ** k > 1:
            low = k
        else:
            high = k
    k = (low + high) / 2
    return q1 ** k, q2 ** k


def pinnacle_probability(row: dict, side: int) -> float | None:
    """The worker's devigged probability, or for rows it did not price (not found on
    Betclic, live there) the same devig of Pinnacle's two prices."""
    stored = number(row.get(f"probability{side}"))
    if stored is not None:
        return stored
    odds = [number(row.get("pinnacle_odds1")), number(row.get("pinnacle_odds2"))]
    if any(value is None or value <= 1 for value in odds):
        return None
    return power_devig(*odds)[side - 1]


def has_started(row: dict, views: dict, captured_at: datetime | None) -> bool:
    """Live on Betclic, or a listed start (Pinnacle's or Polymarket's) before the
    market prices were read: a market then trades in-play, so its price says nothing
    about a prematch book price."""
    if row.get("betclic_status") == "live":
        return True
    if captured_at is None:
        return False
    starts = [parse_time(row.get("pinnacle_starts_at"))]
    starts += [view.get("starts_at") for view in views.values()]
    return any(start is not None and start <= captured_at for start in starts)


def ev(price: float | None, chance: float | None) -> float | None:
    if price is None or chance is None:
        return None
    return price * chance - 1


# ------------------------------------------------------------------ records --


def compare_row(
    row: dict,
    markets: dict[str, list[dict]],
    max_spread: float,
    captured_at: datetime | None = None,
) -> dict:
    """One match: Pinnacle's power-devigged probability, each market's view, and per
    side the best book price and its EV against every reference. A started match
    keeps its prices but gets no EV."""
    found = {}
    for source in SOURCES:
        hit = find_market(row, markets.get(source, []))
        if hit:
            view = market_view(*hit)
            view["tight"] = view["spread"] is not None and view["spread"] <= max_spread + 1e-9
            found[source] = view

    started = has_started(row, found, captured_at)
    sides = {}
    for side in (1, 2):
        prices = book_prices(row, side)
        book = max(prices, key=prices.get) if prices else None
        price = prices.get(book) if book else None
        pinnacle = pinnacle_probability(row, side)
        evs = {"pinnacle": None if started else ev(price, pinnacle)}
        for source in SOURCES:
            chance = probability(found.get(source), side)
            evs[source] = None if started else ev(price, chance)
        sides[side] = {
            "prices": prices, "book": book, "price": price,
            "pinnacle": pinnacle, "evs": evs,
        }

    return {"row": row, "markets": found, "sides": sides, "started": started}


def trusted_evs(match: dict, side: int) -> dict[str, float]:
    """EVs against Pinnacle and against every tight market for one side."""
    evs = match["sides"][side]["evs"]
    trusted = {}
    if evs["pinnacle"] is not None:
        trusted["pinnacle"] = evs["pinnacle"]
    for source, view in match["markets"].items():
        if view["tight"] and evs[source] is not None:
            trusted[source] = evs[source]
    return trusted


def best_side(match: dict) -> int:
    """The side whose best trusted EV is higher; side 1 when neither has one."""
    best = {
        side: max(trusted_evs(match, side).values(), default=None) for side in (1, 2)
    }
    if best[1] is None and best[2] is None:
        return 1
    if best[2] is None:
        return 1
    if best[1] is None:
        return 2
    return 1 if best[1] >= best[2] else 2


def agreement(match: dict, side: int, min_ev: float) -> bool:
    """Pinnacle and every tight market (at least one) give this side EV >= min_ev."""
    trusted = trusted_evs(match, side)
    markets = [source for source in trusted if source != "pinnacle"]
    return (
        "pinnacle" in trusted
        and bool(markets)
        and all(value >= min_ev for value in trusted.values())
    )


def compare_session(
    rows: list[dict], snapshot: list[dict], max_spread: float = DEFAULT_MAX_SPREAD
) -> list[dict]:
    markets = {source: [m for m in snapshot if m.get("source") == source] for source in SOURCES}
    captured_at = snapshot_time(snapshot)
    return [compare_row(row, markets, max_spread, captured_at) for row in rows]


def market_stats(matches: list[dict], min_ev: float) -> dict:
    """Coverage, agreement with Pinnacle and EV counts per source, over prematch
    matches only ("matches"; started ones are counted apart).

    gap = market probability - Pinnacle's, for player 1, tight markets only."""
    prematch = [match for match in matches if not match["started"]]
    stats: dict[str, Any] = {
        "matches": len(prematch),
        "started": len(matches) - len(prematch),
        "sources": {},
    }

    for source in SOURCES:
        matched = [m for m in prematch if source in m["markets"]]
        tight = [m for m in matched if m["markets"][source]["tight"]]
        gaps = []
        flips = 0
        for match in tight:
            pinnacle = match["sides"][1]["pinnacle"]
            market = match["markets"][source]["probability1"]
            if pinnacle is None or market is None:
                continue
            gaps.append(market - pinnacle)
            if (pinnacle - 0.5) * (market - 0.5) < 0:
                flips += 1
        positive = sum(
            any(
                (value := match["sides"][side]["evs"][source]) is not None and value >= min_ev
                for side in (1, 2)
            )
            for match in tight
        )
        stats["sources"][source] = {
            "matched": len(matched),
            "tight": len(tight),
            "compared": len(gaps),
            "median_abs_gap": statistics.median(abs(g) for g in gaps) if gaps else None,
            "within_2pp": sum(abs(g) <= 0.02 for g in gaps),
            "flips": flips,
            "positive": positive,
        }

    stats["pinnacle_positive"] = sum(
        any(
            (value := match["sides"][side]["evs"]["pinnacle"]) is not None and value >= min_ev
            for side in (1, 2)
        )
        for match in matches
    )
    stats["agreeing"] = sum(
        any(agreement(match, side, min_ev) for side in (1, 2)) for match in matches
    )
    return stats


def snapshot_time(snapshot: list[dict]) -> datetime | None:
    times = [parse_time(row.get("captured_at")) for row in snapshot]
    times = [t for t in times if t]
    return max(times) if times else None

