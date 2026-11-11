"""Log NBA prices from every reachable source, for a later edge study.

One run = one snapshot: each source is fetched once (no retries), parsed into
long-format rows (one row per outcome) and written to
``data/nba/snapshots/<UTC date>/<run_id>.csv.gz``. Nothing is compared or sent:
the point is to collect enough history to decide afterwards which reference
(Pinnacle, the average of US books, Kalshi, Polymarket) and which devig method
best predicts closing prices and results.

Sources (each optional; a failure only warns and is recorded in runs.jsonl):

- ``pinnacle``: PinnAPI, basketball (sport_id 3), only with ``PINNAPI_KEY_NBA``
  in .env, a separate key so the tennis quota is untouched. One request.
- ``actionnetwork``: the public JSON behind actionnetwork.com's odds pages:
  US books (DraftKings, FanDuel, BetMGM, Caesars, bet365, BetRivers, Fanatics,
  ...), its consensus and opening line. Answers from the Stockholm VM, but
  returned 403 from a Polish home IP (11 Nov 2026). One request per US date.
- ``espn``: ESPN's scoreboard (DraftKings moneyline, open and current) and
  final scores, which go to ``data/nba/results/espn_results.csv``.
- ``espn_props`` (props runs): DraftKings player props from ESPN's core API
  (over/under and "N+" milestones, with opening prices); athlete names are
  cached in ``data/nba/espn_athletes.json``.
- ``scoresandodds`` (props runs): one page per player on scoresandodds.com
  (about 80 a run, 0.3 s apart): over/under for points, rebounds, assists,
  threes, combos, blocks and steals at bet365, BetMGM, DraftKings, Caesars,
  FanDuel, Hard Rock, Fanatics and Kalshi, plus PrizePicks/Sleeper/Underdog
  lines. Reachable from Poland and the VM (12 Nov 2026).
- ``kalshi``: public market data (no key) for moneyline, spread, total and,
  in props runs, player points/rebounds/assists/threes/PRA ladders.
- ``polymarket``: the Gamma API (no key): moneyline, spreads, totals and, in
  props runs, player points/rebounds/assists/threes. Bid/ask in probability.
- ``superbet``: private collector ``src/betting/books/superbet_nba.py`` (run
  only if the file exists). Prices as the app shows them, before the 12% tax.

Row conventions: ``price`` is a decimal price (sportsbooks); ``bid``/``ask``/
``last`` are probabilities (exchanges, for the outcome in ``side``). ``side``
is a team code for moneyline/spread and over/under for totals and props;
``line`` is the handicap of that side's team, or the total. Kalshi props are
one-way "N+" contracts, stored as ``over`` at line N - 0.5.

Props (player markets) are heavier, so by default they are collected only in
the first run of each hour (``--props auto``).

    python scripts/nba_collect.py [--sources a,b] [--props auto|always|never] [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import html
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

from betting.nba_teams import US_EASTERN, game_date, game_key, team_code  # noqa: E402

DATA_DIR = ROOT / "data" / "nba"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
RESULTS_CSV = DATA_DIR / "results" / "espn_results.csv"
RUNS_LOG = DATA_DIR / "runs.jsonl"
SUPERBET_NBA = ROOT / "src" / "betting" / "books" / "superbet_nba.py"

TIMEOUT = 30
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)

ROW_FIELDS = [
    "run_id", "captured_at", "source", "book", "game_key", "game_date",
    "away", "home", "starts_at", "market", "side", "player", "line",
    "price", "bid", "ask", "last", "volume", "source_id",
]

RESULT_FIELDS = [
    "game_key", "game_date", "away", "home", "starts_at", "status",
    "away_score", "home_score", "winner", "espn_id", "updated_at",
]

# ---------- helpers ----------


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.strip().replace(" ", "T")
    if text.endswith("+00"):
        text += ":00"
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def american_to_decimal(odds: Any) -> float | None:
    try:
        value = float(str(odds).replace("+", ""))
    except (TypeError, ValueError):
        return None
    if value >= 100:
        return round(1 + value / 100, 4)
    if value <= -100:
        return round(1 + 100 / -value, 4)
    return None


def to_float(value: Any) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def make_row(source: str, book: str, away: str, home: str, starts: datetime | None,
             market: str, side: str, *, date: str | None = None, **extra: Any) -> dict:
    date = date or (game_date(starts) if starts else "")
    row = {
        "source": source, "book": book, "away": away, "home": home,
        "game_date": date, "game_key": game_key(date, away, home),
        "starts_at": iso(starts), "market": market, "side": side,
    }
    row.update(extra)
    return row


def get_json(url: str, params: Any = None, headers: dict | None = None) -> Any:
    """One GET. Any non-200 answer raises; there is no retry."""
    response = requests.get(
        url, params=params, timeout=TIMEOUT,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
    )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code} from {url.split('?')[0]}")
    return response.json()


def us_dates(now: datetime, days: tuple[int, ...]) -> list[str]:
    today = now.astimezone(US_EASTERN).date()
    return [(today + timedelta(days=d)).strftime("%Y%m%d") for d in days]


# ---------- Pinnacle (PinnAPI) ----------

PINNAPI_URL = "https://pinnapi.com/kit/v1/prematch/fixtures"
PINNAPI_BASKETBALL = 3


def parse_pinnacle(data: dict) -> list[dict]:
    rows = []
    for ev in data.get("events") or []:
        if ev.get("league_name") != "NBA" or ev.get("parent_id") is not None:
            continue
        home, away = team_code(ev.get("home")), team_code(ev.get("away"))
        if not home or not away:
            continue
        starts = parse_time(ev.get("starts"))
        game = next(
            (p for k, p in (ev.get("periods") or {}).items()
             if p.get("number", k) in (0, "0", "num_0")),
            None,
        )
        if not game:
            continue
        base = dict(source="pinnacle", book="Pinnacle", away=away, home=home,
                    starts=starts, source_id=ev.get("event_id"))
        ml = game.get("money_line") or {}
        for side, key in ((home, "home"), (away, "away")):
            if to_float(ml.get(key)):
                rows.append(make_row(market="moneyline", side=side, price=ml[key], **base))
        for spread in (game.get("spreads") or {}).values():
            hdp = to_float(spread.get("hdp"))
            if hdp is None:
                continue
            rows.append(make_row(market="spread", side=home, line=hdp,
                                 price=spread.get("home"), volume=spread.get("max"), **base))
            rows.append(make_row(market="spread", side=away, line=-hdp,
                                 price=spread.get("away"), volume=spread.get("max"), **base))
        for total in (game.get("totals") or {}).values():
            for side in ("over", "under"):
                rows.append(make_row(market="total", side=side, line=total.get("points"),
                                     price=total.get(side), volume=total.get("max"), **base))
    return rows


def fetch_pinnacle(now: datetime, props: bool) -> list[dict]:
    key = os.environ.get("PINNAPI_KEY_NBA")
    if not key:
        raise SkipSource("PINNAPI_KEY_NBA not set (the tennis key is never used here)")
    response = requests.get(PINNAPI_URL, params={"sport_id": PINNAPI_BASKETBALL},
                            headers={"x-portal-apikey": key}, timeout=TIMEOUT)
    if response.status_code == 429:
        raise RuntimeError("429 from PinnAPI (NBA key quota); not retried")
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code} from PinnAPI")
    return parse_pinnacle(response.json())


# ---------- Action Network ----------

ACTION_URL = "https://api.actionnetwork.com/web/v2/scoreboard/nba"
# Book IDs from its /web/v1/books list. Requested all at once; books it does
# not return for the NBA (on 11 Nov 2026: Pinnacle, Circa, Bovada, BetOnline,
# Kalshi, Superbet PL, Betclic PL) simply give no rows.
ACTION_BOOKS = {
    15: "Consensus", 30: "Open", 68: "DraftKings", 69: "FanDuel", 71: "BetRivers",
    75: "BetMGM", 79: "bet365", 123: "Caesars", 247: "Unibet", 2988: "Fanatics",
    4620: "theScore", 2724: "HardRock", 4795: "Polymarket", 4727: "Kalshi",
    5204: "Novig", 5205: "ProphetX", 3: "Pinnacle", 78: "Circa", 21: "Bovada",
    1: "BetOnline", 2595: "Superbet PL", 2523: "Betclic PL",
}
ACTION_MARKETS = {"moneyline": "moneyline", "spread": "spread", "total": "total"}


def parse_action(data: dict) -> list[dict]:
    rows = []
    for game in data.get("games") or []:
        teams = {t.get("id"): team_code(t.get("abbr")) or team_code(t.get("full_name"))
                 for t in game.get("teams") or []}
        home, away = teams.get(game.get("home_team_id")), teams.get(game.get("away_team_id"))
        if not home or not away:
            continue
        starts = parse_time(game.get("start_time"))
        for book_id, markets in (game.get("markets") or {}).items():
            book = ACTION_BOOKS.get(int(book_id), f"book{book_id}")
            for kind, market in ACTION_MARKETS.items():
                for outcome in (markets.get("event") or {}).get(kind) or []:
                    if outcome.get("is_live"):
                        continue
                    side = outcome.get("side")
                    if side in ("home", "away"):
                        side = home if side == "home" else away
                    price = american_to_decimal(outcome.get("odds"))
                    if price is None:
                        continue
                    rows.append(make_row(
                        "actionnetwork", book, away, home, starts, market, side,
                        line=outcome.get("value") if market != "moneyline" else "",
                        price=price, source_id=game.get("id"),
                    ))
    return rows


def fetch_action(now: datetime, props: bool) -> list[dict]:
    books = ",".join(str(b) for b in ACTION_BOOKS)
    rows = []
    for date in us_dates(now, (0, 1)):
        data = get_json(ACTION_URL, {"period": "game", "bookIds": books, "date": date})
        rows += parse_action(data)
    return rows


# ---------- ESPN ----------

ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"


def parse_espn(data: dict, now: datetime) -> tuple[list[dict], list[dict]]:
    rows, results = [], []
    for event in data.get("events") or []:
        comp = (event.get("competitions") or [{}])[0]
        sides = {c.get("homeAway"): c for c in comp.get("competitors") or []}
        home = team_code((sides.get("home") or {}).get("team", {}).get("abbreviation"))
        away = team_code((sides.get("away") or {}).get("team", {}).get("abbreviation"))
        if not home or not away:
            continue
        starts = parse_time(event.get("date"))
        state = (event.get("status") or {}).get("type") or {}
        if state.get("completed"):
            hs, as_ = to_float(sides["home"].get("score")), to_float(sides["away"].get("score"))
            if hs is not None and as_ is not None:
                date = game_date(starts)
                results.append({
                    "game_key": game_key(date, away, home), "game_date": date,
                    "away": away, "home": home, "starts_at": iso(starts),
                    "status": state.get("name", ""), "away_score": int(as_),
                    "home_score": int(hs), "winner": home if hs > as_ else away,
                    "espn_id": event.get("id"), "updated_at": iso(now),
                })
            continue
        if state.get("state") != "pre":
            continue
        for odds in comp.get("odds") or []:
            provider = (odds.get("provider") or {}).get("name", "ESPN")
            ml = odds.get("moneyline") or {}
            for which, side in (("home", home), ("away", away)):
                for phase, book in (("close", provider), ("open", f"{provider} open")):
                    price = american_to_decimal(((ml.get(which) or {}).get(phase) or {}).get("odds"))
                    if price:
                        rows.append(make_row("espn", book, away, home, starts, "moneyline",
                                             side, price=price, source_id=event.get("id")))
    return rows, results


def fetch_espn(now: datetime, props: bool) -> list[dict]:
    rows, results = [], []
    for date in us_dates(now, (-1, 0, 1)):
        r, res = parse_espn(get_json(ESPN_URL, {"dates": date}), now)
        rows += r
        results += res
    if results:
        save_results(results)
    return rows


def save_results(results: list[dict]) -> None:
    """Upsert finished games by game_key."""
    known: dict[str, dict] = {}
    if RESULTS_CSV.exists():
        with RESULTS_CSV.open(encoding="utf-8", newline="") as f:
            known = {r["game_key"]: r for r in csv.DictReader(f)}
    changed = False
    for r in results:
        old = known.get(r["game_key"])
        if not old or (old["away_score"], old["home_score"]) != (str(r["away_score"]), str(r["home_score"])):
            known[r["game_key"]] = r
            changed = True
    if not changed:
        return
    RESULTS_CSV.parent.mkdir(parents=True, exist_ok=True)
    tmp = RESULTS_CSV.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        writer.writerows(sorted(known.values(), key=lambda r: r["game_key"]))
    tmp.replace(RESULTS_CSV)


# ---------- ESPN player props (DraftKings) ----------

ESPN_CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"
ATHLETE_CACHE = DATA_DIR / "espn_athletes.json"
# ESPN's prop type name -> (market, kind). "ou" items come in pairs, over
# first: on 11 Nov 2026 51 of 52 pairs that had a matching milestone ("23+"
# at the over 22.5 price) put the over first. source_id keeps the position
# (":0"/":1") so the analysis can recheck it.
ESPN_PROP_TYPES = {
    "Total Points": ("player_points", "ou"),
    "Total Rebounds": ("player_rebounds", "ou"),
    "Total Assists": ("player_assists", "ou"),
    "Total 3-Point Field Goals": ("player_threes", "ou"),
    "Total Points, Rebounds, and Assists": ("player_pra", "ou"),
    "Total Points and Rebounds": ("player_pr", "ou"),
    "Total Points and Assists": ("player_pa", "ou"),
    "Total Assists and Rebounds": ("player_ra", "ou"),
    "Total Steals": ("player_steals", "ou"),
    "Points Milestones": ("player_points", "ladder"),
    "Rebounds Milestones": ("player_rebounds", "ladder"),
    "Assists Milestones": ("player_assists", "ladder"),
    "3-Point Field Goals Milestones": ("player_threes", "ladder"),
    "Points + Assists + Rebounds Milestones": ("player_pra", "ladder"),
    "Points + Rebounds Milestones": ("player_pr", "ladder"),
    "Points + Assists Milestones": ("player_pa", "ladder"),
    "Steals Milestones": ("player_steals", "ladder"),
    "Blocks Milestones": ("player_blocks", "ladder"),
}


def athlete_id(item: dict) -> str:
    ref = (item.get("athlete") or {}).get("$ref", "")
    m = re.search(r"/athletes/(\d+)", ref)
    return m.group(1) if m else ""


def parse_espn_props(items: list[dict], names: dict[str, str], away: str, home: str,
                     starts: datetime | None, event_id: str) -> list[dict]:
    rows = []
    pairs: dict[tuple[str, str], list[dict]] = {}
    for item in items:
        kind = ESPN_PROP_TYPES.get((item.get("type") or {}).get("name", ""))
        player = names.get(athlete_id(item), "")
        if not kind or not player:
            continue
        if kind[1] == "ou":
            pairs.setdefault((athlete_id(item), item["type"]["name"]), []).append(item)
            continue
        target = to_float(((item.get("current") or {}).get("target") or {}).get("value"))
        price = to_float((((item.get("odds") or {}).get("decimal")) or {}).get("value"))
        if target is None or not price:
            continue
        rows.append(make_row("espn", "DraftKings", away, home, starts, kind[0], "over",
                             player=player, line=target - 0.5, price=price,
                             source_id=f"{event_id}:ladder"))
    for (aid, type_name), items_ in pairs.items():
        if len(items_) != 2:
            continue
        market = ESPN_PROP_TYPES[type_name][0]
        for pos, (item, side) in enumerate(zip(items_, ("over", "under"))):
            odds = item.get("odds") or {}
            line = to_float((odds.get("total") or {}).get("value"))
            price = to_float((odds.get("decimal") or {}).get("value"))
            open_price = to_float((odds.get("decimal") or {}).get("open"))
            open_line = to_float((odds.get("total") or {}).get("open"))
            if line is None or not price:
                continue
            base = dict(player=names[aid], source_id=f"{event_id}:{aid}:{pos}")
            rows.append(make_row("espn", "DraftKings", away, home, starts, market, side,
                                 line=line, price=price, **base))
            if open_price and open_line is not None and (open_price, open_line) != (price, line):
                rows.append(make_row("espn", "DraftKings open", away, home, starts, market,
                                     side, line=open_line, price=open_price, **base))
    return rows


def athlete_names(ids: set[str]) -> dict[str, str]:
    """Athlete id -> name, cached on disk (one request per new athlete)."""
    names: dict[str, str] = {}
    if ATHLETE_CACHE.exists():
        names = json.loads(ATHLETE_CACHE.read_text(encoding="utf-8"))
    missing = [i for i in ids if i and i not in names]
    for aid in missing:
        try:
            data = get_json(f"{ESPN_CORE}/athletes/{aid}")
            names[aid] = data.get("displayName") or data.get("fullName") or ""
        except Exception as exc:
            print(f"espn_props: athlete {aid} skipped ({exc})")
    if missing:
        ATHLETE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        ATHLETE_CACHE.write_text(json.dumps(names, indent=0), encoding="utf-8")
    return names


def fetch_espn_props(now: datetime, props: bool) -> list[dict]:
    if not props:
        raise SkipSource("props run only")
    games = []
    for date in us_dates(now, (0, 1)):
        for event in get_json(ESPN_URL, {"dates": date}).get("events") or []:
            if ((event.get("status") or {}).get("type") or {}).get("state") != "pre":
                continue
            comp = (event.get("competitions") or [{}])[0]
            sides = {c.get("homeAway"): c for c in comp.get("competitors") or []}
            home = team_code((sides.get("home") or {}).get("team", {}).get("abbreviation"))
            away = team_code((sides.get("away") or {}).get("team", {}).get("abbreviation"))
            starts = parse_time(event.get("date"))
            if home and away and starts and starts <= now + timedelta(hours=36):
                games.append((event["id"], away, home, starts))
    rows = []
    for event_id, away, home, starts in games:
        url = f"{ESPN_CORE}/events/{event_id}/competitions/{event_id}/odds/100/propBets"
        items, page, pages = [], 1, 1
        try:
            while page <= pages and page <= 10:
                data = get_json(url, {"limit": 1000, "page": page})
                items += data.get("items") or []
                pages = data.get("pageCount") or 1
                page += 1
        except RuntimeError as exc:  # 404 = no props listed for that game yet
            if "HTTP 404" not in str(exc):
                raise
        if not items:
            continue
        names = athlete_names({athlete_id(i) for i in items})
        rows += parse_espn_props(items, names, away, home, starts, event_id)
    return rows


# ---------- scoresandodds.com player props (US books) ----------

SAO_URL = "https://www.scoresandodds.com"
SAO_PAUSE_S = 0.3
SAO_MARKETS = {
    "points": "player_points", "rebounds": "player_rebounds", "assists": "player_assists",
    "3-pointers": "player_threes", "points,-rebounds,-&-assists": "player_pra",
    "points-&-rebounds": "player_pr", "points-&-assists": "player_pa",
    "rebounds-&-assists": "player_ra", "blocks": "player_blocks", "steals": "player_steals",
}
SAO_BOOKS = {
    "bet365": "bet365", "betmgm": "BetMGM", "draftkings": "DraftKings", "caesars": "Caesars",
    "fanduel": "FanDuel", "hardrock": "HardRock", "fanatics": "Fanatics", "kalshi": "Kalshi",
    "prizepicks": "PrizePicks", "sleeper": "Sleeper", "underdog": "Underdog",
}
SAO_PLAYER = re.compile(
    r'href="/prop-bets/(\d+)/([a-z0-9\-]+)">([^<]+)</a><br />\s*'
    r'<span class="bold small gray">([A-Z]+) @ ([A-Z]+)</span>')
SAO_VALUE = re.compile(r"^([ou])\s*(\d+(?:\.\d+)?)$")


def get_text(url: str) -> str:
    response = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code} from {url.split('?')[0]}")
    return response.text


def sao_players(page: str) -> list[tuple[str, str, str, str, str]]:
    """(id, slug, name, away, home) for each player on the props list page."""
    seen, players = set(), []
    for pid, slug, name, away, home in SAO_PLAYER.findall(page):
        away_code, home_code = team_code(away), team_code(home)
        if pid not in seen and away_code and home_code:
            seen.add(pid)
            players.append((pid, slug, html.unescape(name).strip(), away_code, home_code))
    return players


def parse_sao_player(page: str, name: str, away: str, home: str, player_id: str) -> list[dict]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(page, "html.parser")
    rows = []
    for body in soup.select('tbody[data-key^="odds-table--"]'):
        key = body["data-key"][len("odds-table--"):]
        market = SAO_MARKETS.get(re.sub(r"-p-\d+$", "", key))
        table = body.find_parent("table")
        if not market or table is None:
            continue
        books = [img.get("alt", "") for img in table.select("th.book-logo img")]
        for tr in body.find_all("tr"):
            cells = tr.find_all("td", class_="game-odds")
            if len(cells) != len(books):
                continue
            for book, cell in zip(books, cells):
                value, odds = cell.select_one(".data-value"), cell.select_one(".data-odds")
                if not value or not odds:
                    continue
                m = SAO_VALUE.match(value.get_text(strip=True).lower())
                text = odds.get_text(strip=True).lower()
                price = 2.0 if text in ("even", "ev") else american_to_decimal(text)
                if not m or not price:
                    continue
                rows.append(make_row(
                    "scoresandodds", SAO_BOOKS.get(book, book), away, home, None, market,
                    "over" if m.group(1) == "o" else "under", date="",
                    player=name, line=float(m.group(2)), price=price, source_id=player_id,
                ))
    return rows


def fetch_scoresandodds(now: datetime, props: bool) -> list[dict]:
    if not props:
        raise SkipSource("props run only")
    players = sao_players(get_text(f"{SAO_URL}/nba/props"))
    rows, failed = [], 0
    for pid, slug, name, away, home in players:
        time.sleep(SAO_PAUSE_S)
        try:
            rows += parse_sao_player(get_text(f"{SAO_URL}/prop-bets/{pid}/{slug}"),
                                     name, away, home, pid)
        except Exception as exc:
            failed += 1
            if failed >= 5:  # the site is refusing us: stop, keep what we have
                print(f"scoresandodds: stopped after 5 failures ({exc})")
                break
    return rows


# ---------- Kalshi ----------

KALSHI_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
KALSHI_MAIN = {"KXNBAGAME": "moneyline", "KXNBASPREAD": "spread", "KXNBATOTAL": "total"}
KALSHI_PROPS = {
    "KXNBAPTS": "player_points", "KXNBAREB": "player_rebounds",
    "KXNBAAST": "player_assists", "KXNBA3PT": "player_threes", "KXNBAPRA": "player_pra",
}
KALSHI_EVENT = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})([A-Z]{3})([A-Z]{3})$")
MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}


def kalshi_game(event_ticker: str) -> tuple[str, str, str] | None:
    """(US date, away, home) from e.g. KXNBAGAME-26NOV15SASUTA."""
    m = KALSHI_EVENT.search(event_ticker or "")
    if not m or m.group(2) not in MONTHS:
        return None
    away, home = team_code(m.group(4)), team_code(m.group(5))
    if not away or not home:
        return None
    date = f"20{m.group(1)}-{MONTHS[m.group(2)]:02d}-{m.group(3)}"
    return date, away, home


def parse_kalshi(markets: list[dict], market: str) -> list[dict]:
    rows = []
    for m in markets:
        game = kalshi_game(m.get("event_ticker", ""))
        if not game:
            continue
        date, away, home = game
        suffix = m.get("ticker", "").rsplit("-", 1)[-1]
        strike = to_float(m.get("floor_strike"))
        player, line = "", ""
        if market == "moneyline":
            side = team_code(suffix)
        elif market == "spread":
            side = team_code(suffix[:3])
            line = -strike if strike is not None else ""
        elif market == "total":
            side, line = "over", strike
        else:
            side, line = "over", strike
            player = (m.get("title") or "").split(":")[0].strip()
        if not side or (market != "moneyline" and line in ("", None)):
            continue
        # occurrence_datetime is the expected end (start + 3 h on 11 Nov 2026),
        # so the start is taken from another source by fill_starts.
        starts = None
        rows.append(make_row(
            "kalshi", "Kalshi", away, home, starts, market, side, date=date,
            player=player, line=line,
            bid=to_float(m.get("yes_bid_dollars")), ask=to_float(m.get("yes_ask_dollars")),
            last=to_float(m.get("last_price_dollars")), volume=to_float(m.get("volume_fp")),
            source_id=m.get("ticker"),
        ))
    return rows


def kalshi_series(series: str) -> list[dict]:
    markets, cursor = [], None
    for _ in range(20):
        params = {"series_ticker": series, "status": "open", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = get_json(KALSHI_URL, params)
        markets += data.get("markets") or []
        cursor = data.get("cursor")
        if not cursor:
            break
    return markets


def fetch_kalshi(now: datetime, props: bool) -> list[dict]:
    rows = []
    series = {**KALSHI_MAIN, **(KALSHI_PROPS if props else {})}
    for ticker, market in series.items():
        rows += parse_kalshi(kalshi_series(ticker), market)
    return rows


# ---------- Polymarket ----------

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
POLY_NBA_TAG = 745
POLY_MAIN = ("moneyline", "spreads", "totals")
POLY_PROPS = {"points": "player_points", "rebounds": "player_rebounds",
              "assists": "player_assists", "threes": "player_threes"}
POLY_SLUG = re.compile(r"^nba-([a-z]+)-([a-z]+)-(\d{4}-\d{2}-\d{2})")
NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")


def parse_polymarket(markets: list[dict]) -> list[dict]:
    rows = []
    for m in markets:
        kind = m.get("sportsMarketType")
        slug = ((m.get("events") or [{}])[0] or {}).get("slug") or m.get("slug", "")
        g = POLY_SLUG.match(slug)
        if not g:
            continue
        away, home, date = team_code(g.group(1).upper()), team_code(g.group(2).upper()), g.group(3)
        if not away or not home:
            continue
        try:
            outcomes = json.loads(m.get("outcomes") or "[]")
            prices = [to_float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
        except (TypeError, ValueError):
            continue
        if len(outcomes) != 2 or len(prices) != 2:
            continue
        bid, ask = to_float(m.get("bestBid")), to_float(m.get("bestAsk"))
        quotes = [(bid, ask), (1 - ask if ask is not None else None, 1 - bid if bid is not None else None)]
        question = m.get("question") or ""
        line = to_float(m.get("line"))
        if line is None:
            found = NUMBER.findall(question.split(":")[-1])
            line = to_float(found[-1]) if found else None
        player = ""
        if kind == "moneyline":
            market, sides, lines = "moneyline", [team_code(o) for o in outcomes], ["", ""]
        elif kind == "spreads":
            if line is None:
                continue
            market, sides, lines = "spread", [team_code(o) for o in outcomes], [line, -line]
        elif kind == "totals":
            market, sides, lines = "total", ["over", "under"], [line, line]
        elif kind in POLY_PROPS:
            market, sides, lines = POLY_PROPS[kind], ["over", "under"], [line, line]
            player = question.split(":")[0].strip()
        else:
            continue
        if not all(sides) or (market != "moneyline" and line is None):
            continue
        starts = parse_time(m.get("gameStartTime"))
        for i in range(2):
            rows.append(make_row(
                "polymarket", "Polymarket", away, home, starts, market, sides[i], date=date,
                player=player, line=lines[i], bid=quotes[i][0], ask=quotes[i][1],
                last=prices[i], volume=to_float(m.get("volumeNum")), source_id=m.get("id"),
            ))
    return rows


def poly_markets(types: tuple[str, ...], now: datetime) -> list[dict]:
    """Every open NBA market of these types closing in the next 3 days (pages of 100)."""
    markets = []
    for offset in range(0, 5000, 100):
        params = [("closed", "false"), ("tag_id", POLY_NBA_TAG), ("limit", 100),
                  ("offset", offset), ("end_date_min", iso(now)),
                  ("end_date_max", iso(now + timedelta(days=3)))]
        params += [("sports_market_types", t) for t in types]
        page = get_json(GAMMA_URL, params)
        markets += page
        if len(page) < 100:
            break
    return markets


def fetch_polymarket(now: datetime, props: bool) -> list[dict]:
    """One query per market type: the API answered 422 to an offset past 2000
    (12 Nov 2026), which all prop types together reached (~2100 markets)."""
    rows = []
    for kind in POLY_MAIN:
        rows += parse_polymarket(poly_markets((kind,), now))
    if props:
        for kind in POLY_PROPS:
            try:
                rows += parse_polymarket(poly_markets((kind,), now))
            except Exception as exc:  # keep the game lines if one prop type fails
                print(f"polymarket: {kind} props skipped ({type(exc).__name__}: {exc})")
    return rows


# ---------- Superbet (private) ----------


def fetch_superbet(now: datetime, props: bool) -> list[dict]:
    if not SUPERBET_NBA.exists():
        raise SkipSource(f"{SUPERBET_NBA.relative_to(ROOT)} not found")
    spec = importlib.util.spec_from_file_location("superbet_nba", SUPERBET_NBA)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.collect(now, props)


# ---------- run ----------


class SkipSource(Exception):
    """A source that is not configured here (not an error)."""


SOURCES: dict[str, Callable[[datetime, bool], list[dict]]] = {
    "pinnacle": fetch_pinnacle,
    "actionnetwork": fetch_action,
    "espn": fetch_espn,
    "espn_props": fetch_espn_props,
    "scoresandodds": fetch_scoresandodds,
    "kalshi": fetch_kalshi,
    "polymarket": fetch_polymarket,
    "superbet": fetch_superbet,
}


def resolve_dates(rows: list[dict], now: datetime) -> None:
    """Rows that only know their teams (scoresandodds) get the game_date of the
    nearest game with those teams that another source lists from today on,
    else today's US date."""
    today = now.astimezone(US_EASTERN).strftime("%Y-%m-%d")
    dates: dict[tuple[str, str], list[str]] = {}
    for r in rows:
        if r.get("game_date"):
            dates.setdefault((r["away"], r["home"]), []).append(r["game_date"])
    for r in rows:
        if not r.get("game_date"):
            later = sorted(d for d in dates.get((r["away"], r["home"]), []) if d >= today)
            r["game_date"] = later[0] if later else today
            r["game_key"] = game_key(r["game_date"], r["away"], r["home"])


def fill_starts(rows: list[dict]) -> None:
    """Give rows without a start time the one another source has for that game."""
    starts: dict[str, str] = {}
    for source in ("pinnacle", "espn", "actionnetwork", "superbet", "polymarket", "kalshi"):
        for r in rows:
            if r["source"] == source and r.get("starts_at"):
                starts.setdefault(r["game_key"], r["starts_at"])
    for r in rows:
        if not r.get("starts_at"):
            r["starts_at"] = starts.get(r["game_key"], "")


def prematch_only(rows: list[dict], now: datetime) -> list[dict]:
    """Drop games that have started (in-play prices are not what we study).

    Rows still without a start (a Kalshi game no other source lists) are kept
    if their US date is today or later.
    """
    stamp, today = iso(now), now.astimezone(US_EASTERN).strftime("%Y-%m-%d")
    def upcoming(r: dict) -> bool:
        if r.get("starts_at"):
            return r["starts_at"] > stamp
        return r.get("game_date", "") >= today

    return [r for r in rows if upcoming(r)]


def write_snapshot(rows: list[dict], run_id: str, now: datetime) -> Path:
    folder = SNAPSHOT_DIR / now.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{run_id}.csv.gz"
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ROW_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def want_props(mode: str, now: datetime) -> bool:
    return mode == "always" or (mode == "auto" and now.minute < 15)


def run(sources: list[str], props_mode: str, dry_run: bool) -> int:
    now = utc_now()
    run_id = f"nba_{now:%Y%m%d_%H%M%S}"
    props = want_props(props_mode, now)
    all_rows: list[dict] = []
    report: dict[str, Any] = {"run_id": run_id, "captured_at": iso(now), "props": props, "sources": {}}
    for name in sources:
        started = time.monotonic()
        entry: dict[str, Any] = {}
        try:
            rows = SOURCES[name](now, props)
            for r in rows:
                r.setdefault("source", name)
            all_rows += rows
            entry["rows"] = len(rows)
        except SkipSource as exc:
            entry["skipped"] = str(exc)
        except Exception as exc:  # one source failing must not lose the others
            entry["error"] = f"{type(exc).__name__}: {exc}"
            if os.environ.get("NBA_DEBUG"):
                traceback.print_exc()
        entry["seconds"] = round(time.monotonic() - started, 2)
        report["sources"][name] = entry
        status = entry.get("error") or entry.get("skipped") or f"{entry['rows']} rows"
        print(f"{name:14s} {entry['seconds']:6.2f}s  {status}")

    for r in all_rows:
        r["run_id"], r["captured_at"] = run_id, iso(now)
    resolve_dates(all_rows, now)
    fill_starts(all_rows)
    kept = prematch_only(all_rows, now)
    report["rows"] = len(kept)
    report["games"] = len({r["game_key"] for r in kept})
    print(f"{len(kept)} prematch rows, {report['games']} games, props={'yes' if props else 'no'}")
    if dry_run:
        return 0
    if kept:
        report["file"] = str(write_snapshot(kept, run_id, now).relative_to(ROOT))
    RUNS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RUNS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(report) + "\n")
    failed = [n for n, e in report["sources"].items() if "error" in e]
    return 1 if failed and len(failed) == len([n for n, e in report["sources"].items() if "skipped" not in e]) else 0


def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sources", default=",".join(SOURCES),
                        help="comma-separated subset of: " + ", ".join(SOURCES))
    parser.add_argument("--props", choices=("auto", "always", "never"), default="auto",
                        help="player markets: auto = first run of each hour (minute < 15)")
    parser.add_argument("--dry-run", action="store_true", help="fetch and count, write nothing")
    args = parser.parse_args()
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    unknown = [s for s in sources if s not in SOURCES]
    if unknown:
        parser.error(f"unknown source(s): {', '.join(unknown)}")
    return run(sources, args.props, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
