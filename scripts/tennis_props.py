"""
Tennis prices beyond the match winner (sets, games, handicaps, totals), logged for a later EV study.

Since 25 Nov 2026 (user's request): like scripts/nba_collect.py, data collection only.
Nothing here is compared, alerted or counted on the Tennis page; the point is enough
history to tell later whether the soft books' set and games markets are +EV against
Pinnacle, and which reference predicts closes and results. The dashboard's Props
subtab (scripts/dashboard_props.py) shows the newest snapshot.

One run = one snapshot of every source (no retries; a failing source only warns) to
data/tennis_props/snapshots/<UTC date>/<run_id>.csv.gz in long format (ROW_FIELDS),
one line per run in data/tennis_props/runs.jsonl. Every row is keyed to a Pinnacle
match (`pinnacle_event_id`) and uses Pinnacle's player order: `side` is p1/p2 for
player markets, over/under for totals, "2-0" style set scores from player 1's view.
`line` is that side's handicap, or the total. Matches are kept only before their
start and within HORIZON_HOURS of it.

Markets (MARKETS): match_winner, set1_winner, set2_winner, set_handicap, total_sets,
game_handicap, total_games, set1_game_handicap, set1_total_games, set2_total_games,
p1_games / p2_games (one player's games, over/under), set_score.

Sources:
- pinnacle: PinnFeed, one request, every market (the 5-minute run asks for the
  moneyline only). The match event ("Sets") holds the match and first-set moneylines,
  set handicap and total sets; its "(Games)" child (parentId) the games handicap and
  total for the match (period 0) and the first set (period 1). Main lines only on the
  free plan. Total games can be a whole number (push possible). 96 requests a day at
  15-minute runs, next to the tennis runs' 288 and the NBA log's 96 (of 1,000).
- superbet / betclic: private collectors src/betting/books/superbet_tennis_props.py and
  src/betting/books/betclic_tennis_props.py (run only if present), called as
  ``collect(now, matches, session_id)``. They take the Pinnacle links of the newest
  finished tennis session (`link_session`) and read fresh prices for those matches.
  Superbet prices as the app shows them (before the 0.88 tax), Betclic's with the tax.
- polymarket: the Gamma tennis events tennis_markets.py reads, every market type in
  POLY_MARKETS; bid/ask per side (side 2 = 1 - the other side's ask/bid).
- kalshi: set-winner series (KALSHI_SERIES; on 25 Nov 2026 only the Challenger ones had
  open markets), one market per player, paired by event ticker.

Usage:
    python scripts/tennis_props.py [--sources a,b] [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import importlib.util
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "src", ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import market_compare  # noqa: E402
import tennis_markets  # noqa: E402

DATA_DIR = ROOT / "data" / "tennis_props"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
RUNS_LOG = DATA_DIR / "runs.jsonl"
SESSIONS_DIR = ROOT / "data" / "results" / "sessions"
BOOKS_DIR = ROOT / "src" / "betting" / "books"
SUPERBET_PROPS = BOOKS_DIR / "superbet_tennis_props.py"
BETCLIC_PROPS = BOOKS_DIR / "betclic_tennis_props.py"

TIMEOUT = 30
HORIZON_HOURS = 36
# The tennis session whose book links are used: the newest finished one at most this old.
LINK_MAX_AGE = timedelta(minutes=20)

PINNFEED_URL = "https://pinnfeed.io/v1/odds"
PINNFEED_PARAMS = {"sport": "tennis", "live": "false", "limit": 1000}

KALSHI_SERIES = (
    "KXATPSETWINNER", "KXWTASETWINNER",
    "KXATPCHALLENGERSETWINNER", "KXWTACHALLENGERSETWINNER",
)
KALSHI_SET = re.compile(r"wins Set (\d)", re.I)

MARKETS = (
    "match_winner", "set1_winner", "set2_winner", "set_handicap", "total_sets",
    "game_handicap", "total_games", "set1_game_handicap", "set1_total_games",
    "set2_total_games", "p1_games", "p2_games", "set_score",
)

ROW_FIELDS = [
    "run_id", "captured_at", "source", "book", "pinnacle_event_id", "starts_at",
    "tournament", "player1", "player2", "market", "side", "line", "price",
    "bid", "ask", "volume", "source_id",
]

# (type, period, games child) -> market
PINNACLE_MARKETS = {
    ("moneyline", 0, False): "match_winner",
    ("moneyline", 1, False): "set1_winner",
    ("spread", 0, False): "set_handicap",
    ("total", 0, False): "total_sets",
    ("spread", 0, True): "game_handicap",
    ("total", 0, True): "total_games",
    ("spread", 1, True): "set1_game_handicap",
    ("total", 1, True): "set1_total_games",
}

# sportsMarketType -> market; a None line is read from the market
POLY_MARKETS = {
    "moneyline": "match_winner",
    "tennis_first_set_winner": "set1_winner",
    "tennis_set_winner": "set_winner",  # set number in `line`
    "tennis_set_handicap": "set_handicap",
    "tennis_game_handicap": "game_handicap",
    "tennis_set_totals": "total_sets",
    "tennis_match_totals": "total_games",
    "tennis_first_set_totals": "set1_total_games",
    "tennis_set_games_totals": "set2_total_games",
}


class SkipSource(Exception):
    """A source that is not configured here (not an error)."""


# ---------- helpers ----------


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


parse_time = market_compare.parse_time
number = market_compare.number


def row(match: dict, source: str, book: str, market: str, side: str, **extra: Any) -> dict:
    out = {
        "source": source, "book": book,
        "pinnacle_event_id": match["pinnacle_event_id"], "starts_at": match["starts_at"],
        "tournament": match["tournament"], "player1": match["player1"],
        "player2": match["player2"], "market": market, "side": side,
    }
    out.update(extra)
    return out


def flip_side(side: str) -> str:
    """The same outcome seen from the other player (p1 <-> p2, 2-0 <-> 0-2)."""
    if side in ("p1", "p2"):
        return "p2" if side == "p1" else "p1"
    if re.fullmatch(r"\d-\d", side):
        return side[::-1]
    return side


# ---------- Pinnacle ----------


def parse_pinnacle(data: dict, now: datetime) -> tuple[list[dict], list[dict]]:
    """(matches, rows). A match = a prematch "Sets" event with a match moneyline,
    starting within HORIZON_HOURS; its "(Games)" child adds the games markets."""
    events = data.get("events") or []
    horizon = now + timedelta(hours=HORIZON_HOURS)
    matches: dict[Any, dict] = {}
    for ev in events:
        if ev.get("parentId") is not None or not ev.get("home") or not ev.get("away"):
            continue
        if ev.get("live") or str(ev.get("status") or "").lower() == "started":
            continue
        starts = parse_time(ev.get("startTime"))
        if starts is None or not (now < starts <= horizon):
            continue
        if not any(m.get("type") == "moneyline" and m.get("period") in (0, "0")
                   for m in ev.get("markets") or []):
            continue
        matches[ev.get("id")] = {
            "pinnacle_event_id": ev.get("id"), "starts_at": iso(starts),
            "tournament": (ev.get("league") or {}).get("name") or "",
            "player1": ev["home"], "player2": ev["away"],
        }

    rows = []
    for ev in events:
        games = ev.get("parentId") is not None
        match = matches.get(ev.get("parentId") if games else ev.get("id"))
        if match is None:
            continue
        for market in ev.get("markets") or []:
            try:
                period = int(market.get("period"))
            except (TypeError, ValueError):
                continue
            name = PINNACLE_MARKETS.get((market.get("type"), period, games))
            if name is None or market.get("alternate"):
                continue
            for outcome in market.get("outcomes") or []:
                side = {"home": "p1", "away": "p2"}.get(outcome.get("name"), outcome.get("name"))
                price = number(outcome.get("decimal"))
                if side not in ("p1", "p2", "over", "under") or not price or price <= 1:
                    continue
                line = number(outcome.get("points"))
                if market.get("type") != "moneyline" and line is None:
                    continue
                rows.append(row(match, "pinnacle", "Pinnacle", name, side,
                                line="" if line is None else line, price=price,
                                volume=market.get("maxStake") or "",
                                source_id=market.get("id") or ""))
    return list(matches.values()), rows


def fetch_pinnacle(now: datetime) -> dict:
    key = (os.environ.get("PINNFEED_KEY") or "").strip()
    if not key:
        raise SkipSource("PINNFEED_KEY not set")
    response = requests.get(PINNFEED_URL, params=PINNFEED_PARAMS,
                            headers={"x-api-key": key}, timeout=TIMEOUT)
    if response.status_code == 429:
        raise RuntimeError("429 from PinnFeed (daily quota); not retried")
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code} from PinnFeed")
    return response.json()


# ---------- prediction markets ----------


def pair_rows(match: dict, source: str, market: str, sides: tuple[str, str],
              quotes: tuple[tuple, tuple], line: tuple = ("", ""), volume: Any = "",
              source_id: str = "") -> list[dict]:
    """Two rows, one per side; quotes = ((bid, ask), (bid, ask)), missing = None."""
    out = []
    for side, (bid, ask), each_line in zip(sides, quotes, line):
        if bid is None and ask is None:
            continue
        out.append(row(match, source, source.capitalize(), market, side,
                       line=each_line, bid="" if bid is None else bid,
                       ask="" if ask is None else ask, volume=volume, source_id=source_id))
    return out


def poly_market_rows(market: dict, match: dict) -> list[dict]:
    name = POLY_MARKETS.get(market.get("sportsMarketType") or "")
    if name is None or market.get("closed"):
        return []
    try:
        outcomes = json.loads(market.get("outcomes") or "[]")
    except ValueError:
        return []
    if len(outcomes) != 2:
        return []
    bid, ask = number(market.get("bestBid")), number(market.get("bestAsk"))
    if not bid:
        bid = None  # no bid at all
    if ask is not None and ask >= 1:
        ask = None  # nobody sells
    quotes = (
        (bid, ask),
        (None if ask is None else round(1 - ask, 4), None if bid is None else round(1 - bid, 4)),
    )
    line = number(market.get("line"))
    volume = number(market.get("liquidityNum")) or ""
    source_id = str(market.get("id") or "")

    if name == "set_winner":  # the set number is the line; set 1 has its own type
        if line != 2:
            return []
        name, line = "set2_winner", None
    if name.startswith("total") or name.endswith("total_games"):
        if line is None:
            return []
        sides = tuple("over" if o.lower().startswith("over") else "under" for o in outcomes)
        if set(sides) != {"over", "under"}:
            return []
        return pair_rows(match, "polymarket", name, sides, quotes, (line, line), volume, source_id)

    # Player outcomes: match them to Pinnacle's players.
    p1, p2 = match["player1"], match["player2"]
    if market_compare.same_team(p1, outcomes[0]) and market_compare.same_team(p2, outcomes[1]):
        sides = ("p1", "p2")
    elif market_compare.same_team(p2, outcomes[0]) and market_compare.same_team(p1, outcomes[1]):
        sides = ("p2", "p1")
    else:
        return []
    lines: tuple = ("", "")
    if name.endswith("handicap"):
        if line is None:
            return []
        lines = (line, -line)  # the line is the first outcome's handicap
    return pair_rows(match, "polymarket", name, sides, quotes, lines, volume, source_id)


def parse_polymarket(events: list[dict], matches: list[dict]) -> list[dict]:
    """Every listed market of each event that matches a Pinnacle match (both names,
    start within market_compare.MAX_START_GAP_HOURS)."""
    rows = []
    for event in events:
        moneyline = next((m for m in event.get("markets") or []
                          if m.get("sportsMarketType") == "moneyline"), None)
        if moneyline is None:
            continue
        try:
            players = json.loads(moneyline.get("outcomes") or "[]")
        except ValueError:
            continue
        if len(players) != 2:
            continue
        candidate = {
            "player1": players[0], "player2": players[1],
            "starts_at": event.get("startTime") or moneyline.get("gameStartTime"),
        }
        match = matching(candidate, matches)
        if match is None:
            continue
        for market in event.get("markets") or []:
            rows += poly_market_rows(market, match)
    return rows


def matching(candidate: dict, matches: list[dict]) -> dict | None:
    """The Pinnacle match a market's two players belong to (market_compare's rules)."""
    found = None
    for match in matches:
        probe = {"pinnacle_player1": match["player1"], "pinnacle_player2": match["player2"],
                 "pinnacle_starts_at": match["starts_at"]}
        if market_compare.find_market(probe, [candidate]):
            if found is not None:
                return None  # two Pinnacle matches fit: leave it out
            found = match
    return found


def fetch_polymarket(now: datetime, matches: list[dict], session_id: str | None) -> list[dict]:
    return parse_polymarket(tennis_markets.polymarket_events(now), matches)


def parse_kalshi(markets: list[dict], matches: list[dict]) -> list[dict]:
    by_event: dict[str, list[dict]] = {}
    for market in markets:
        if market.get("status") in (None, "active", "open"):
            by_event.setdefault(market.get("event_ticker") or "", []).append(market)
    rows = []
    for event, pair in sorted(by_event.items()):
        if not event or len(pair) != 2:
            continue
        found = KALSHI_SET.search(pair[0].get("title") or "")
        if not found or found.group(1) not in ("1", "2"):
            continue
        candidate = {"player1": pair[0].get("yes_sub_title") or "",
                     "player2": pair[1].get("yes_sub_title") or "", "starts_at": ""}
        match = matching(candidate, matches)
        if match is None:
            continue
        order = market_compare.orientation((match["player1"], match["player2"]), candidate)
        sides = ("p1", "p2") if order == 1 else ("p2", "p1")

        def quote(market: dict) -> tuple:
            bid = number(market.get("yes_bid_dollars")) or None
            ask = number(market.get("yes_ask_dollars"))
            return bid, (ask if ask and ask < 1 else None)

        volume = sum(number(m.get("volume_fp")) or 0 for m in pair)
        rows += pair_rows(match, "kalshi", f"set{found.group(1)}_winner", sides,
                          (quote(pair[0]), quote(pair[1])), volume=volume, source_id=event)
    return rows


def fetch_kalshi(now: datetime, matches: list[dict], session_id: str | None) -> list[dict]:
    markets, failures = [], []
    for series in KALSHI_SERIES:
        try:
            data = tennis_markets.get_json(
                tennis_markets.KALSHI_URL,
                {"series_ticker": series, "status": "open", "limit": 1000},
            )
        except (requests.RequestException, ValueError) as exc:
            failures.append(f"{series}: {type(exc).__name__}")
            continue
        markets += data.get("markets") or []
    if len(failures) == len(KALSHI_SERIES):
        raise RuntimeError("every series failed: " + ", ".join(failures))
    return parse_kalshi(markets, matches)


# ---------- private bookmaker collectors (gitignored) ----------


def run_private(path: Path, now: datetime, matches: list[dict], session_id: str | None) -> list[dict]:
    if not path.exists():
        raise SkipSource(f"{path.relative_to(ROOT)} not found")
    if session_id is None:
        raise RuntimeError(f"no finished tennis session in the last {LINK_MAX_AGE}")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    by_id = {str(m["pinnacle_event_id"]): m for m in matches}
    rows = []
    for found in module.collect(now, matches, session_id):
        match = by_id.get(str(found.get("pinnacle_event_id")))
        if match is not None:
            rows.append(row(match, **{k: v for k, v in found.items() if k != "pinnacle_event_id"}))
    return rows


def fetch_superbet(now, matches, session_id) -> list[dict]:
    return run_private(SUPERBET_PROPS, now, matches, session_id)


def fetch_betclic(now, matches, session_id) -> list[dict]:
    return run_private(BETCLIC_PROPS, now, matches, session_id)


def link_session(now: datetime, sessions_dir: Path = SESSIONS_DIR) -> str | None:
    """The newest finished tennis session started within LINK_MAX_AGE: its book files
    say which Superbet event and Betclic match belong to each Pinnacle match."""
    for path in sorted(sessions_dir.glob("session_*_status.json"), reverse=True)[:12]:
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        started = parse_time(info.get("session_start"))
        if started is None or now - started > LINK_MAX_AGE:
            return None
        if info.get("status") == "finished":
            return info.get("session_id") or path.name[: -len("_status.json")]
    return None


# ---------- run ----------

SOURCES: dict[str, Callable[[datetime, list[dict], str | None], list[dict]]] = {
    "superbet": fetch_superbet,
    "betclic": fetch_betclic,
    "polymarket": fetch_polymarket,
    "kalshi": fetch_kalshi,
}


def write_snapshot(rows: list[dict], run_id: str, now: datetime) -> Path:
    folder = SNAPSHOT_DIR / now.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{run_id}.csv.gz"
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ROW_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def run(sources: list[str], dry_run: bool) -> int:
    now = utc_now()
    run_id = f"props_{now:%Y%m%d_%H%M%S}"
    report: dict[str, Any] = {"run_id": run_id, "captured_at": iso(now), "sources": {}}

    started = time.monotonic()
    try:
        matches, rows = parse_pinnacle(fetch_pinnacle(now), now)
        entry: dict[str, Any] = {"rows": len(rows), "matches": len(matches)}
    except SkipSource as exc:
        matches, rows, entry = [], [], {"skipped": str(exc)}
    except Exception as exc:  # the others still need matches, so nothing more to do
        matches, rows, entry = [], [], {"error": f"{type(exc).__name__}: {exc}"}
    entry["seconds"] = round(time.monotonic() - started, 2)
    report["sources"]["pinnacle"] = entry
    print(f"{'pinnacle':11s} {entry['seconds']:6.2f}s  "
          f"{entry.get('error') or entry.get('skipped') or f'{len(rows)} rows, {len(matches)} matches'}",
          flush=True)

    session_id = link_session(now)
    report["link_session"] = session_id
    for name in sources:
        started = time.monotonic()
        entry = {}
        try:
            if not matches:
                raise SkipSource("no Pinnacle matches")
            found = SOURCES[name](now, matches, session_id)
            for r in found:
                r.setdefault("source", name)
            rows += found
            entry["rows"] = len(found)
            entry["matches"] = len({r["pinnacle_event_id"] for r in found})
        except SkipSource as exc:
            entry["skipped"] = str(exc)
        except Exception as exc:  # one source failing must not lose the others
            entry["error"] = f"{type(exc).__name__}: {exc}"
            if os.environ.get("PROPS_DEBUG"):
                traceback.print_exc()
        entry["seconds"] = round(time.monotonic() - started, 2)
        report["sources"][name] = entry
        status = entry.get("error") or entry.get("skipped") or (
            f"{entry['rows']} rows, {entry['matches']} matches")
        print(f"{name:11s} {entry['seconds']:6.2f}s  {status}", flush=True)

    for r in rows:
        r["run_id"], r["captured_at"] = run_id, iso(now)
    report["rows"] = len(rows)
    report["matches"] = len(matches)
    print(f"{len(rows)} rows for {len(matches)} Pinnacle matches (links: {session_id})", flush=True)
    if dry_run:
        return 0
    if rows:
        report["file"] = str(write_snapshot(rows, run_id, now).relative_to(ROOT))
    RUNS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RUNS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(report) + "\n")
    return 0 if rows else 1


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--sources", default=",".join(SOURCES),
                        help="comma-separated subset of: " + ", ".join(SOURCES)
                        + " (Pinnacle always runs: every row is keyed to it)")
    parser.add_argument("--dry-run", action="store_true", help="fetch and count, write nothing")
    args = parser.parse_args()
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    unknown = [s for s in sources if s not in SOURCES]
    if unknown:
        parser.error(f"unknown source(s): {', '.join(unknown)}")
    return run(sources, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
