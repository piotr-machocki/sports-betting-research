"""Fetch finished tennis matches and link them to Pinnacle rows.

Results live apart from the odds and are joined only when analysing:
    data/match_results/raw/<date>_<tour>.json - provider rows per day (merged by id)
    data/match_results/results.csv            - one clean row per match
    data/match_results/state.json             - last day fully fetched

run_once.py calls update() after each session. It makes no request while
yesterday (UTC) is already covered, so it fetches at most once a day. It fetches
from the last covered day, and always the last RECHECK_DAYS, because late matches
appear after midnight. A rate limit is reported, never retried.

Usage:
    python scripts/match_results.py                     # same step as run_once.py
    python scripts/match_results.py --from 2026-10-01   # fetch further back
    python scripts/match_results.py --report            # coverage, no requests
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import unicodedata

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import requests

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]

load_dotenv(ROOT / ".env")

API_HOST = "tennis-api-atp-wta-itf.p.rapidapi.com"
TOURS = ("atp", "wta")  # atp includes Challenger + ITF men, wta includes ITF women
PAGE_SIZE = 500

CHUNK_DAYS = 7
RECHECK_DAYS = 3
MAX_REQUESTS_PER_RUN = 30  # the free plan allows 50 a day
RESERVE_REQUESTS = 5  # stop when the daily quota gets this low

DATA_DIR = ROOT / "data" / "match_results"
RAW_DIR = DATA_DIR / "raw"
RESULTS_CSV = DATA_DIR / "results.csv"
STATE_FILE = DATA_DIR / "state.json"

PINNACLE_RAW_DIR = ROOT / "data" / "pinnacle" / "raw"
PROCESSED_ODDS = ROOT / "data" / "results" / "processed_odds.csv"

RESULT_FIELDS = [
    "match_id", "tour", "date", "tournament", "round_id",
    "winner", "loser", "result", "result_type", "is_doubles",
    "odds_winner", "odds_loser",
]

# How far a played match can be from the day we expect it, in days.
WINDOW_FROM_START = (-1, 2)  # scheduled start known; it can move
WINDOW_FROM_CAPTURE = (-1, 5)  # only the snapshot time known

FUZZY_TOKEN_RATIO = 0.75
CONFIDENCE_ORDER = ("exact", "partial", "fuzzy")

# Words of a tournament name that say nothing about where it is played.
GENERIC_TOURNAMENT_WORDS = frozenset(
    "atp wta itf men women challenger open masters ladies tennis championships "
    "doubles qualifiers final semi quarter sf qf k".split()
)

# Letters that NFKD does not reduce to ASCII.
SPECIAL_LETTERS = str.maketrans(
    {"ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ß": "ss", "ı": "i"}
)


class FetchStopped(Exception):
    """The provider refused, or this run's request budget is spent."""


# ---------- fetching ----------

class Client:
    """Counts requests against a budget. Every request is a single attempt."""

    def __init__(self, api_key: str, max_requests: int) -> None:
        self.api_key = api_key
        self.left = max_requests
        self.used = 0

    def get_page(self, tour: str, start: date, stop: date, page: int) -> dict[str, Any]:
        if self.left <= 0:
            raise FetchStopped("request budget reached; the rest waits for the next run")

        self.left -= 1
        self.used += 1

        r = requests.get(
            f"https://{API_HOST}/tennis/v2/{tour}/results/{start}/{stop}",
            params={"pageSize": PAGE_SIZE, "pageNo": page},
            headers={"X-RapidAPI-Key": self.api_key, "X-RapidAPI-Host": API_HOST},
            timeout=30,
        )

        if r.status_code == 429:
            raise FetchStopped("429: daily limit reached; no retry was made")
        if r.status_code in (401, 403):
            raise FetchStopped(f"{r.status_code}: check RAPIDAPI_KEY and the API subscription")
        r.raise_for_status()

        remaining = r.headers.get("X-RateLimit-Requests-Remaining")
        if remaining is not None and remaining.isdigit():
            self.left = min(self.left, int(remaining) - RESERVE_REQUESTS)

        return r.json()


def fetch_tour(client: Client, tour: str, start: date, stop: date) -> list[dict[str, Any]]:
    """All provider rows of one tour for a date range (inclusive)."""
    rows: list[dict[str, Any]] = []
    page = 1

    while True:
        body = client.get_page(tour, start, stop, page)
        rows.extend(body.get("data") or [])

        if not body.get("hasNextPage"):
            return rows

        page += 1


def date_chunks(start: date, stop: date) -> list[tuple[date, date]]:
    chunks = []

    while start <= stop:
        end = min(start + timedelta(days=CHUNK_DAYS - 1), stop)
        chunks.append((start, end))
        start = end + timedelta(days=1)

    return chunks


# ---------- storing ----------

def write_atomic(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, path)


def read_covered_through() -> date | None:
    if not STATE_FILE.exists():
        return None

    value = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("covered_through")
    return date.fromisoformat(value) if value else None


def write_state(covered: date | None) -> None:
    write_atomic(
        STATE_FILE,
        json.dumps(
            {
                "covered_through": covered.isoformat() if covered else None,
                "last_fetch_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        ),
    )


def store_rows(rows: list[dict[str, Any]], tour: str) -> None:
    """Merge into per-day files by match id, so a later fetch loses nothing."""
    by_day: dict[str, list[dict[str, Any]]] = {}

    for row in rows:
        by_day.setdefault(str(row.get("date"))[:10], []).append(row)

    for day, day_rows in by_day.items():
        path = RAW_DIR / f"{day}_{tour}.json"
        merged = {}

        if path.exists():
            merged = {
                str(r["id"]): r for r in json.loads(path.read_text(encoding="utf-8"))
            }

        merged.update({str(r["id"]): r for r in day_rows})
        write_atomic(path, json.dumps(list(merged.values()), ensure_ascii=False))


def parse_row(row: dict[str, Any], tour: str) -> dict[str, Any] | None:
    """One clean result row. None when the provider names no winner."""
    p1, p2 = row.get("player1") or {}, row.get("player2") or {}
    winner_id = row.get("match_winner")

    if winner_id == row.get("player1Id"):
        winner, loser, odds_w, odds_l = p1, p2, row.get("odd1"), row.get("odd2")
    elif winner_id == row.get("player2Id"):
        winner, loser, odds_w, odds_l = p2, p1, row.get("odd2"), row.get("odd1")
    else:
        return None

    return {
        "match_id": row.get("id"),
        "tour": tour,
        "date": str(row.get("date"))[:10],
        "tournament": (row.get("tournament") or {}).get("name"),
        "round_id": row.get("roundId"),
        "winner": winner.get("name"),
        "loser": loser.get("name"),
        "result": row.get("result"),
        "result_type": row.get("result_type"),
        "is_doubles": "/" in str(winner.get("name")),
        "odds_winner": odds_w,
        "odds_loser": odds_l,
    }


def rebuild_csv() -> int:
    """Rebuild results.csv from the raw files. Returns its number of rows."""
    rows = []

    for path in sorted(RAW_DIR.glob("*.json")):
        tour = path.stem.rsplit("_", 1)[1]
        parsed = (parse_row(r, tour) for r in json.loads(path.read_text(encoding="utf-8")))
        rows.extend(r for r in parsed if r is not None)

    tmp = RESULTS_CSV.with_suffix(".csv.tmp")
    RESULTS_CSV.parent.mkdir(parents=True, exist_ok=True)

    with tmp.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    os.replace(tmp, RESULTS_CSV)
    return len(rows)


# ---------- updating ----------

def first_snapshot_date() -> date | None:
    """Day of the oldest Pinnacle snapshot: nothing before it can be linked."""
    days = []

    for path in PINNACLE_RAW_DIR.glob("*.csv"):
        found = re.search(r"(\d{8})", path.stem)
        if found:
            days.append(datetime.strptime(found.group(1), "%Y%m%d").date())

    return min(days) if days else None


def start_date(covered: date | None, yesterday: date, first: date | None) -> date:
    recheck_from = yesterday - timedelta(days=RECHECK_DAYS - 1)

    if covered is not None:
        return min(covered + timedelta(days=1), recheck_from)

    return min(first, recheck_from) if first else recheck_from


def update(
    today: date | None = None,
    start: date | None = None,
    max_requests: int = MAX_REQUESTS_PER_RUN,
) -> dict[str, Any]:
    """Fetch what is missing. Returns a summary; provider refusals do not raise."""
    today = today or datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    covered = read_covered_through()

    if start is None and covered is not None and covered >= yesterday:
        return {"status": "up to date", "covered_through": covered, "requests": 0}

    api_key = os.getenv("RAPIDAPI_KEY")
    if not api_key:
        return {"status": "no RAPIDAPI_KEY in .env", "covered_through": covered, "requests": 0}

    client = Client(api_key, max_requests)
    start = start or start_date(covered, yesterday, first_snapshot_date())
    status = "updated"

    try:
        for chunk_start, chunk_stop in date_chunks(start, yesterday):
            found = 0

            for tour in TOURS:
                rows = fetch_tour(client, tour, chunk_start, chunk_stop)
                store_rows(rows, tour)
                found += len(rows)

            # An empty week means the provider failed us, not that nobody played.
            contiguous = covered is None or chunk_start <= covered + timedelta(days=1)
            if found and contiguous and (covered is None or chunk_stop > covered):
                covered = chunk_stop

            write_state(covered)

    except FetchStopped as exc:
        status = f"stopped early: {exc}"

    finally:
        total = rebuild_csv() if RAW_DIR.exists() else 0

    return {
        "status": status,
        "covered_through": covered,
        "requests": client.used,
        "results": total,
    }


def run_step() -> None:
    """The run_once.py step. Never raises: a run must not lose data over this."""
    try:
        summary = update()
        line = f"Match results:     {summary['status']}, "
        line += f"covered through {summary['covered_through']}, "
        line += f"{summary['requests']} requests"
        print(line, flush=True)

    except Exception as exc:
        print(
            f"WARNING: could not update match results: {type(exc).__name__}: {exc}",
            flush=True,
        )


# ---------- linking ----------

def name_tokens(name: str) -> frozenset[str]:
    text = str(name).translate(SPECIAL_LETTERS)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[-.]", " ", text)
    return frozenset(re.sub(r"[^a-z ]", "", text).split())


def player_quality(a: frozenset[str], b: frozenset[str]) -> str | None:
    """How well two names agree, ignoring order (Asian names flip) and middle names."""
    if not a or not b:
        return None
    if a == b:
        return "exact"
    if len(a & b) >= 2 and (a <= b or b <= a):
        return "partial"
    if a & b and is_spelling_variant(a, b):
        return "fuzzy"
    return None


def is_spelling_variant(a: frozenset[str], b: frozenset[str]) -> bool:
    """Same letters split differently (Yi Fan / Yifan), or close differing words."""
    if sorted("".join(a)) == sorted("".join(b)):
        return True

    rest_a, rest_b = sorted(a - b), sorted(b - a)

    return len(rest_a) == len(rest_b) and all(
        SequenceMatcher(None, x, y).ratio() >= FUZZY_TOKEN_RATIO
        for x, y in zip(rest_a, rest_b)
    )


def tournament_words(name: str) -> frozenset[str]:
    """Place words of a tournament name, for telling rematches apart."""
    words = name_tokens(name) - GENERIC_TOURNAMENT_WORDS
    return frozenset(w for w in words if len(w) > 1)  # digits are gone: R1 -> r, W35 -> w


def team_tokens(name: str) -> tuple[frozenset[str], ...]:
    """A singles player, or both doubles partners in a fixed order."""
    return tuple(sorted((name_tokens(part) for part in str(name).split("/")), key=sorted))


def side_quality(a: tuple[frozenset[str], ...], b: tuple[frozenset[str], ...]) -> str | None:
    if len(a) != len(b):
        return None

    ranks = []

    for pair in ([*zip(a, b)], [*zip(a, reversed(b))]):
        qualities = [player_quality(x, y) for x, y in pair]
        if all(qualities):
            ranks.append(max(CONFIDENCE_ORDER.index(q) for q in qualities))

    return CONFIDENCE_ORDER[min(ranks)] if ranks else None


def index_results(results: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Results by day, with parsed names, ready for link_match."""
    by_day: dict[str, list[dict[str, Any]]] = {}

    for row in results:
        if not row.get("winner") or not row.get("loser"):
            continue

        by_day.setdefault(row["date"], []).append(
            {**row, "_w": team_tokens(row["winner"]), "_l": team_tokens(row["loser"])}
        )

    return by_day


def link_match(
    player1: str,
    player2: str,
    by_day: dict[str, list[dict[str, Any]]],
    starts_at: str | None = None,
    captured_at: str | None = None,
    tournament: str | None = None,
) -> dict[str, Any]:
    """Find a Pinnacle match among the results.

    Returns {"linked": False, "reason": ...} or {"linked": True, "result": row,
    "winner_side": 1|2, "confidence": ...}. The same pair can meet twice in a few
    days, so the same tournament wins first, then the closer day; equally good
    results that disagree on the winner are left unlinked.
    """
    reference = starts_at or captured_at
    if not reference:
        return {"linked": False, "reason": "no date"}

    expected = date.fromisoformat(str(reference)[:10])
    low, high = WINDOW_FROM_START if starts_at else WINDOW_FROM_CAPTURE

    p1, p2 = team_tokens(player1), team_tokens(player2)
    place = tournament_words(tournament) if tournament else frozenset()
    candidates = []

    for offset in range(low, high + 1):
        day = (expected + timedelta(days=offset)).isoformat()

        for row in by_day.get(day, []):
            for side, (first, second) in ((1, (row["_w"], row["_l"])), (2, (row["_l"], row["_w"]))):
                q1 = side_quality(p1, first)
                q2 = side_quality(p2, second) if q1 else None

                if q1 and q2:
                    rank = max(CONFIDENCE_ORDER.index(q1), CONFIDENCE_ORDER.index(q2))
                    other_place = not place & tournament_words(row.get("tournament") or "")
                    candidates.append((other_place, abs(offset), rank, side, row))

    if not candidates:
        return {"linked": False, "reason": "no result"}

    candidates.sort(key=lambda c: c[:3])
    best = candidates[0]

    if any(c[:3] == best[:3] and c[3] != best[3] for c in candidates[1:]):
        return {"linked": False, "reason": "ambiguous"}

    return {
        "linked": True,
        "result": {k: v for k, v in best[4].items() if not k.startswith("_")},
        "winner_side": best[3],
        "confidence": CONFIDENCE_ORDER[best[2]],
    }


def load_results() -> list[dict[str, str]]:
    with RESULTS_CSV.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


# ---------- coverage report ----------

def tour_category(tournament: str) -> str:
    """Tour label of a Pinnacle tournament name."""
    if "challenger" in tournament.lower():
        return "Challenger"
    return tournament.split()[0] if tournament else "?"


def unique_snapshot_matches() -> list[dict[str, str]]:
    """Each Pinnacle match once, keeping its last capture and latest known start."""
    matches: dict[tuple, dict[str, str]] = {}

    for path in sorted(PINNACLE_RAW_DIR.glob("*.csv")):
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                pair = frozenset((name_tokens(row["player1"]), name_tokens(row["player2"])))
                key = (pair, row.get("tournament"))
                seen = matches.setdefault(key, {**row, "last_captured": row["captured_at"]})
                seen["last_captured"] = max(seen["last_captured"], row["captured_at"])
                if row.get("starts_at"):
                    seen["starts_at"] = row["starts_at"]

    return list(matches.values())


def coverage_report() -> None:
    covered = read_covered_through()
    by_day = index_results(load_results())
    print(f"Results: {sum(map(len, by_day.values()))}, covered through {covered}")

    totals: Counter[str] = Counter()
    linked: Counter[str] = Counter()
    quality: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    misses = []

    for match in unique_snapshot_matches():
        # Matches still listed on the last covered day may be played the day after.
        if covered is None or match["last_captured"][:10] >= covered.isoformat():
            continue

        category = tour_category(match["tournament"])
        if "/" in match["player1"]:
            category += " doubles"

        outcome = link_match(
            match["player1"],
            match["player2"],
            by_day,
            match.get("starts_at") or None,
            match["last_captured"],
            match["tournament"],
        )
        totals[category] += 1

        if outcome["linked"]:
            linked[category] += 1
            quality[outcome["confidence"]] += 1
        else:
            reasons[outcome["reason"]] += 1
            misses.append(f"{match['player1']} - {match['player2']} ({match['tournament']})")

    for category in sorted(totals):
        share = linked[category] / totals[category]
        print(f"  {category:<20}{linked[category]:>5} / {totals[category]:<5} {share:6.1%}")

    print(f"  {'all':<20}{sum(linked.values()):>5} / {sum(totals.values()):<5}", end=" ")
    print(f"{sum(linked.values()) / max(sum(totals.values()), 1):6.1%}")
    print(f"Link quality: {dict(quality)}")
    print(f"Not linked:   {dict(reasons)}")

    for miss in misses[:15]:
        print(f"  {miss}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="start", type=date.fromisoformat, default=None,
                        help="fetch from this day (YYYY-MM-DD), even if already covered")
    parser.add_argument("--max-requests", type=int, default=MAX_REQUESTS_PER_RUN)
    parser.add_argument("--report", action="store_true",
                        help="print coverage of the Pinnacle snapshots; makes no requests")
    args = parser.parse_args()

    if args.report:
        coverage_report()
        return

    print(update(start=args.start, max_requests=args.max_requests))


if __name__ == "__main__":
    main()
