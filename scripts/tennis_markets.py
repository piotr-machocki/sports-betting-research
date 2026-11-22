"""
Tennis match-winner prices from prediction markets (Polymarket, Kalshi), saved for comparison.

Since 20 Nov 2026 (user's request). Nothing here drives EV or alerts: the files are a
second, unproven reference, kept so that it can later be compared with Pinnacle's
devigged prices, closing lines and results before anyone trusts it. Both APIs are
public, need no account or key, and answered from Poland and the VM.

One run = one snapshot of both sources to
data/markets/tennis/<UTC date>/<run_id>.csv.gz, one row per match and source, in the
market's own player order. No retries; a failing source only warns and the other is
still saved. Exit code 1 only when neither gave a row.

Prices are probabilities in dollars per $1 contract (0-1). For player 2:
Polymarket quotes one order book per match, so bid2 = 1 - ask1 and ask2 = 1 - bid1;
Kalshi has one market per player, so each side has its own book and they need not
add up. Buying at the ask is what a bet would cost, so 1 / ask is the decimal price
and ask1 + ask2 - 1 is the overround.

Sources (probed 20 Nov 2026):
- Polymarket Gamma /events, tag 864 (Tennis), pages of 100 (above 50 it serves gzip
  only); old unresolved events stay open for weeks, so only events whose start is at
  most STARTED_HOURS back are kept. Each event has one market with
  sportsMarketType "moneyline" (plus set markets). 95 upcoming matches, median spread
  2 cents, median liquidity about $6k, ITF included.
- Kalshi /markets per series in KALSHI_SERIES, one market per player ("X wins"),
  paired by event_ticker. No start time: expected_expiration_time is stored instead.
  25 open singles matches, spreads 1-2 cents, many with almost no volume.

Usage:
    python scripts/tennis_markets.py [--run-id ID] [--show N]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "markets" / "tennis"

TIMEOUT = 20
USER_AGENT = "sports-betting-research/1.0"

GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
POLY_TENNIS_TAG = 864
POLY_PAGE = 100
POLY_MAX_PAGES = 10
STARTED_HOURS = 6

KALSHI_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
KALSHI_SERIES = (
    "KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH",
    "KXCHALLENGERMATCH", "KXITFMATCH", "KXITFWMATCH",
    "KXATPDOUBLES", "KXWTADOUBLES", "KXATPCHALLENGERDOUBLES", "KXITFDOUBLES", "KXITFWDOUBLES",
)
KALSHI_MAX_PAGES = 5
KALSHI_TOURNAMENT = re.compile(r"\bin the (.+?) after a ball has been played")

ROW_FIELDS = [
    "source", "captured_at", "event_id", "series", "tournament", "starts_at",
    "player1", "player2", "bid1", "ask1", "bid2", "ask2", "last1", "last2",
    "volume", "liquidity",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        value = datetime.fromisoformat(str(text).replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso(value: datetime | None) -> str:
    return value.isoformat().replace("+00:00", "Z") if value else ""


def get_json(url: str, params: dict) -> object:
    response = requests.get(
        url, params=params, timeout=TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
    )
    response.raise_for_status()
    return response.json()


# ------------------------------------------------------------------ Polymarket --


def parse_polymarket(events: list[dict], captured_at: datetime) -> list[dict]:
    """One row per event with an open two-way moneyline market that started at most
    STARTED_HOURS ago (or has no start)."""
    rows = []

    for event in events:
        starts = parse_time(event.get("startTime"))
        if starts and starts < captured_at - timedelta(hours=STARTED_HOURS):
            continue

        for market in event.get("markets") or []:
            if market.get("sportsMarketType") != "moneyline" or market.get("closed"):
                continue

            try:
                players = json.loads(market.get("outcomes") or "[]")
                last = json.loads(market.get("outcomePrices") or "[]")
            except ValueError:
                continue

            bid, ask = to_float(market.get("bestBid")), to_float(market.get("bestAsk"))
            if len(players) != 2:
                continue

            title = event.get("title") or market.get("question") or ""
            rows.append({
                "source": "polymarket",
                "captured_at": iso(captured_at),
                "event_id": event.get("slug") or event.get("id") or "",
                "series": event.get("seriesSlug") or "",
                "tournament": title.split(":", 1)[0] if ":" in title else "",
                "starts_at": iso(starts or parse_time(market.get("gameStartTime"))),
                "player1": players[0],
                "player2": players[1],
                "bid1": bid if bid is not None else "",
                "ask1": ask if ask is not None else "",
                "bid2": round(1 - ask, 4) if ask is not None else "",
                "ask2": round(1 - bid, 4) if bid is not None else "",
                "last1": to_float(last[0]) if len(last) == 2 else "",
                "last2": to_float(last[1]) if len(last) == 2 else "",
                "volume": to_float(market.get("volumeNum")) or "",
                "liquidity": to_float(market.get("liquidityNum")) or "",
            })
            break

    return rows


def fetch_polymarket(now: datetime) -> list[dict]:
    events: list[dict] = []
    since = iso(now - timedelta(hours=STARTED_HOURS))

    for page in range(POLY_MAX_PAGES):
        batch = get_json(GAMMA_EVENTS, {
            "tag_id": POLY_TENNIS_TAG, "closed": "false", "limit": POLY_PAGE,
            "offset": page * POLY_PAGE, "end_date_min": since,
        })
        if not isinstance(batch, list):
            raise ValueError(f"unexpected reply: {str(batch)[:120]}")
        events += batch
        if len(batch) < POLY_PAGE:
            break

    return parse_polymarket(events, now)


# ---------------------------------------------------------------------- Kalshi --


def parse_kalshi(markets: list[dict], series: str, captured_at: datetime) -> list[dict]:
    """Pair the per-player markets of each event; events without exactly two are left out."""
    by_event: dict[str, list[dict]] = {}
    for market in markets:
        if market.get("status") not in (None, "active", "open"):
            continue
        by_event.setdefault(market.get("event_ticker") or "", []).append(market)

    rows = []
    for event, pair in sorted(by_event.items()):
        if not event or len(pair) != 2:
            continue
        first, second = sorted(pair, key=lambda market: market.get("ticker") or "")
        tournament = KALSHI_TOURNAMENT.search(first.get("rules_primary") or "")
        volume = sum(to_float(m.get("volume_fp")) or 0 for m in pair)

        def price(market: dict, field: str):
            value = to_float(market.get(f"{field}_dollars"))
            return value if value else ""  # 0 = no order on that side

        rows.append({
            "source": "kalshi",
            "captured_at": iso(captured_at),
            "event_id": event,
            "series": series,
            "tournament": tournament.group(1) if tournament else "",
            "starts_at": "",  # Kalshi gives no start; see expected expiration in event_id's date
            "player1": first.get("yes_sub_title") or "",
            "player2": second.get("yes_sub_title") or "",
            "bid1": price(first, "yes_bid"),
            "ask1": price(first, "yes_ask"),
            "bid2": price(second, "yes_bid"),
            "ask2": price(second, "yes_ask"),
            "last1": price(first, "last_price"),
            "last2": price(second, "last_price"),
            "volume": volume,
            "liquidity": "",
        })

    return rows


def fetch_kalshi(now: datetime) -> list[dict]:
    rows: list[dict] = []
    failures = []

    for series in KALSHI_SERIES:
        markets: list[dict] = []
        cursor = ""
        try:
            for _ in range(KALSHI_MAX_PAGES):
                params = {"series_ticker": series, "status": "open", "limit": 1000}
                if cursor:
                    params["cursor"] = cursor
                data = get_json(KALSHI_URL, params)
                markets += data.get("markets") or []
                cursor = data.get("cursor") or ""
                if not cursor:
                    break
        except (requests.RequestException, ValueError) as exc:
            failures.append(f"{series}: {type(exc).__name__}")
            continue
        rows += parse_kalshi(markets, series, now)

    if failures and len(failures) == len(KALSHI_SERIES):
        raise RuntimeError("every series failed: " + ", ".join(failures))
    for failure in failures:
        print(f"WARNING: Kalshi {failure}", flush=True)

    return rows


# ------------------------------------------------------------------------ main --


SOURCES = {"polymarket": fetch_polymarket, "kalshi": fetch_kalshi}


def write_snapshot(rows: list[dict], run_id: str, now: datetime) -> Path:
    folder = OUT_DIR / f"{now:%Y-%m-%d}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{run_id}.csv.gz"
    with gzip.open(path, "wt", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=ROW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def fetch_all(now: datetime) -> dict[str, list[dict] | Exception]:
    """Every source at once, one thread each (user, 22 Nov 2026): one after the other
    the second was read about 2 s later (Polymarket 1.9 s, Kalshi 1.5 s on the VM).
    A source that raises gives its exception, so the other is still kept."""
    def one(fetch) -> list[dict] | Exception:
        try:
            return fetch(now)
        except Exception as exc:  # noqa: BLE001 - one source never stops the other
            return exc

    with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        futures = {name: pool.submit(one, fetch) for name, fetch in SOURCES.items()}
        return {name: future.result() for name, future in futures.items()}


def run(run_id: str, show: int = 0) -> int:
    now = utc_now()
    rows: list[dict] = []

    for name, found in fetch_all(now).items():
        if isinstance(found, Exception):
            print(f"WARNING: {name} failed: {type(found).__name__}: {found}", flush=True)
            continue
        priced = sum(bool(row["ask1"] and row["ask2"]) for row in found)
        print(f"{name}: {len(found)} matches, {priced} with both asks", flush=True)
        rows += found

    if not rows:
        print("ERROR: no prediction-market rows", flush=True)
        return 1

    print(f"Prediction markets saved to: {write_snapshot(rows, run_id, now)}", flush=True)

    for row in rows[:show]:
        print(f"  {row['source']:<10} {row['player1'][:24]:<24} {row['ask1']!s:>6} | "
              f"{row['player2'][:24]:<24} {row['ask2']!s:>6}  {row['tournament'][:30]}")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--run-id", help="Default: manual_<UTC time>.")
    parser.add_argument("--show", type=int, default=0, help="Print the first N rows.")
    args = parser.parse_args()
    return run(args.run_id or f"manual_{utc_now():%Y%m%d_%H%M%S}", args.show)


if __name__ == "__main__":
    sys.exit(main())
