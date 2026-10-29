import csv
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from dotenv import load_dotenv

import requests

load_dotenv()

BASE_URL = "https://pinnapi.com"
PATH_PREMATCH = "/kit/v1/prematch/fixtures"
TENNIS_SPORT_ID = 2

# pinnacle.py -> books -> betting -> src -> root repo
BASE_DIR = Path(__file__).resolve().parents[3]

BOOK_DATA_DIR = BASE_DIR / "data" / "pinnacle"
RAW_DIR = BOOK_DATA_DIR / "raw"
JSON_DIR = BOOK_DATA_DIR / "json"

LATEST_CSV = BOOK_DATA_DIR / "odds.csv"
LATEST_JSON = JSON_DIR / "latest.json"

CSV_FIELDS = [
    "player1", "odds1", "player2", "odds2",
    "tournament", "round", "status", "captured_at",
]

# Why an event did not become a match. Every event gets exactly one outcome.
# EXCLUDED: not a full-match winner market (expected, not a data problem).
# INVALID:  looks like a match but its data is unusable.
EXCLUDED_REASONS = ("child_event", "no_odds", "no_full_match_market", "no_money_line")
INVALID_REASONS = ("missing_players", "invalid_odds")


# ---------- fetching ----------

def fetch_events(api_key: str, retries: int = 3) -> dict[str, Any]:
    for _ in range(retries):
        r = requests.get(
            BASE_URL + PATH_PREMATCH,
            params={"sport_id": TENNIS_SPORT_ID},
            headers={"x-portal-apikey": api_key},
            timeout=30,
        )
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", "30"))
            print(f"429 rate limit, waiting {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue
        if r.status_code == 401:
            sys.exit("401: invalid or missing API key.")
        if r.status_code == 503:
            sys.exit("503: prematch feed disabled on pinnapi side.")
        r.raise_for_status()
        try:
            return r.json()
        except requests.exceptions.JSONDecodeError as exc:
            raise RuntimeError("PinnAPI returned an invalid JSON response.") from exc
    sys.exit("Failed to fetch data (rate limit).")


# ---------- parsing ----------

def parse_event(
    ev: dict[str, Any], captured_at: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Return (match, None) for a valid event, else (None, rejection_reason)."""
    # "(Games)" events have parent_id - they are games markets, not the match.
    if ev.get("parent_id") is not None:
        return None, "child_event"
    if not ev.get("is_have_odds", True):
        return None, "no_odds"

    # Period 0 = whole match.
    full_match = None
    for key, per in (ev.get("periods") or {}).items():
        if per.get("number", key) in (0, "0", "num_0"):
            full_match = per
            break
    if not full_match:
        return None, "no_full_match_market"

    ml = full_match.get("money_line") or {}
    home_odds, away_odds = ml.get("home"), ml.get("away")
    home, away = ev.get("home"), ev.get("away")

    if not home or not away:
        return None, "missing_players"
    if home_odds is None and away_odds is None:
        return None, "no_money_line"
    try:
        home_odds, away_odds = float(home_odds), float(away_odds)
    except (TypeError, ValueError):
        return None, "invalid_odds"
    if home_odds <= 1 or away_odds <= 1:
        return None, "invalid_odds"

    return {
        "player1": home,
        "odds1": home_odds,
        "player2": away,
        "odds2": away_odds,
        "tournament": ev.get("league_name"),
        "round": None,  # Pinnacle does not provide the round
        "status": ev.get("event_type") or "prematch",
        "captured_at": captured_at,
    }, None


def parse_pinnacle_matches_detailed(
    data: dict[str, Any], captured_at: str
) -> tuple[list[dict[str, Any]], Counter]:
    """Extract tennis match-winner (moneyline) odds plus rejection counts."""
    matches = []
    rejected: Counter = Counter()

    for ev in data.get("events", []):
        match, reason = parse_event(ev, captured_at)
        if match is None:
            rejected[reason] += 1
        else:
            matches.append(match)

    return matches, rejected


def parse_pinnacle_matches(data: dict[str, Any], captured_at: str) -> list[dict[str, Any]]:
    """Extract tennis match-winner (moneyline) odds from pinnapi response."""
    return parse_pinnacle_matches_detailed(data, captured_at)[0]


# ---------- pipeline ----------

def main() -> dict[str, Any]:
    api_key = os.environ.get("PINNAPI_KEY")
    if not api_key:
        sys.exit("Set the PINNAPI_KEY environment variable.")

    BOOK_DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    JSON_DIR.mkdir(parents=True, exist_ok=True)

    fetch_started = time.perf_counter()
    data = fetch_events(api_key)
    fetch_seconds = time.perf_counter() - fetch_started

    now = datetime.now(timezone.utc)
    captured_at = now.isoformat()
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")

    # Save raw response before parsing, for debugging.
    LATEST_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    matches, rejected = parse_pinnacle_matches_detailed(data, captured_at)
    if not matches:
        raise RuntimeError(f"No matches parsed. CSV not saved, inspect {LATEST_JSON}")

    csv_path = RAW_DIR / f"odds_{stamp}.csv"

    # Historical snapshot - never overwrite.
    with open(csv_path, "x", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(matches)

    # Latest data - replace previous file.
    with open(LATEST_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(matches)

    print(f"Events in response: {len(data.get('events', []))}")
    print(f"Parsed {len(matches)} matches")
    print(f"Latest CSV: {LATEST_CSV}")
    print(f"Historical snapshot: {csv_path}")

    return {
        "events": len(data.get("events", [])),
        "matches": len(matches),
        "api_seconds": fetch_seconds,
        "excluded": {r: rejected[r] for r in EXCLUDED_REASONS if rejected[r]},
        "invalid": {r: rejected[r] for r in INVALID_REASONS if rejected[r]},
    }


if __name__ == "__main__":
    main()