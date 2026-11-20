"""
Pinnacle tennis match-winner odds through PinnFeed (pinnfeed.io), one request per run.

Since 20 Nov 2026. PinnAPI, the earlier source, suspended every account on 17 Nov 2026.
PinnFeed's free plan: REST only, prices 30 s delayed, 2 sports (tennis is one), main
markets only, 1,000 requests a day for all keys of an account, 1 request a second.
One GET of /v1/odds returns every prematch tennis event (288 on 20 Nov 2026, 119 with
a match moneyline), so runs every 5 minutes use 288 a day. Use one account and one key
(PINNFEED_KEY): the limit counts the account, and its terms forbid sharing keys to get
around limits.

Output is unchanged from the PinnAPI version: data/pinnacle/odds.csv (latest) and
data/pinnacle/raw/odds_<stamp>.csv (never overwritten), columns CSV_FIELDS, with the
raw reply in data/pinnacle/json/latest.json. Match winner = the moneyline market of
period 0 (period 1 is the first set); "(Games)" child events have a parentId and no
moneyline. Unlike PinnAPI, PinnFeed says when a match has started (status "started"):
such rows get status "live", so the collectors skip them and no EV is computed for a
match already under way.

A 429, 401 or 403 exits at once without retrying, so no more of the quota is used.
"""

import csv
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://pinnfeed.io/v1"
ODDS_PARAMS = {"sport": "tennis", "market": "moneyline", "live": "false", "limit": 1000}
KEY_VARIABLE = "PINNFEED_KEY"

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
    "event_id", "starts_at",
]

# Why an event did not become a match. Every event gets exactly one outcome.
# EXCLUDED: not a full-match winner market (expected, not a data problem).
# INVALID:  looks like a match but its data is unusable.
EXCLUDED_REASONS = ("child_event", "no_odds", "no_full_match_market", "no_money_line")
INVALID_REASONS = ("missing_players", "invalid_odds")


# ---------- fetching ----------

def api_key() -> str | None:
    return (os.environ.get(KEY_VARIABLE) or "").strip() or None


def error_text(response: requests.Response) -> str:
    """PinnFeed's own error message, never the key."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error") or "")[:200]
    return ""


def fetch_events(key: str) -> tuple[dict[str, Any], dict[str, str]]:
    """One request per run: (reply, quota headers). Never retried."""
    r = requests.get(
        f"{BASE_URL}/odds", params=ODDS_PARAMS, headers={"x-api-key": key}, timeout=30,
    )
    detail = error_text(r) if r.status_code >= 400 else ""
    if r.status_code == 429:
        sys.exit(
            "429: PinnFeed rate limit reached. No retry was made, so no more of the "
            f"quota is used. {detail}".strip()
        )
    if r.status_code in (401, 403):
        sys.exit(f"{r.status_code}: PinnFeed refused the key. {detail}".strip())
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError as exc:
        raise RuntimeError("PinnFeed returned an invalid JSON response.") from exc
    quota = {
        name: r.headers.get(header, "")
        for name, header in (("limit", "X-Ratelimit-Limit"), ("remaining", "X-Ratelimit-Remaining"),
                             ("delay_seconds", "X-Data-Delay-Seconds"))
    }
    return data, quota


# ---------- parsing ----------

def match_moneyline(ev: dict[str, Any]) -> dict[str, Any] | None:
    for market in ev.get("markets") or []:
        if market.get("type") == "moneyline" and market.get("period") in (0, "0"):
            return market
    return None


def parse_event(
    ev: dict[str, Any], captured_at: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Return (match, None) for a valid event, else (None, rejection_reason)."""
    # "(Games)" events have a parentId: games markets, not the match.
    if ev.get("parentId") is not None:
        return None, "child_event"
    if not ev.get("markets"):
        return None, "no_odds"

    market = match_moneyline(ev)
    if market is None:
        return None, "no_full_match_market"

    prices = {o.get("name"): o.get("decimal") for o in market.get("outcomes") or []}
    home_odds, away_odds = prices.get("home"), prices.get("away")
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

    started = ev.get("live") or str(ev.get("status") or "").lower() == "started"

    return {
        "player1": home,
        "odds1": home_odds,
        "player2": away,
        "odds2": away_odds,
        "tournament": (ev.get("league") or {}).get("name"),
        "round": None,  # Pinnacle does not provide the round
        "status": "live" if started else "prematch",
        "captured_at": captured_at,
        "event_id": ev.get("id"),
        "starts_at": ev.get("startTime"),  # scheduled start (UTC); it can move
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
    return parse_pinnacle_matches_detailed(data, captured_at)[0]


# ---------- pipeline ----------

def main() -> dict[str, Any]:
    key = api_key()
    if not key:
        sys.exit(f"Set the {KEY_VARIABLE} environment variable.")

    BOOK_DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    JSON_DIR.mkdir(parents=True, exist_ok=True)

    fetch_started = time.perf_counter()
    data, quota = fetch_events(key)
    fetch_seconds = time.perf_counter() - fetch_started

    now = datetime.now(timezone.utc)
    captured_at = now.isoformat()
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")

    # Save raw response before parsing, for debugging.
    LATEST_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    events = data.get("events", [])
    if isinstance(data.get("count"), int) and data["count"] > len(events):
        print(f"WARNING: PinnFeed has {data['count']} events, the reply held {len(events)}.")

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

    started = sum(match["status"] == "live" for match in matches)
    print(f"Events in response: {len(events)}")
    print(f"Parsed {len(matches)} matches ({started} already started, marked live)")
    if quota.get("remaining"):
        print(f"PinnFeed requests left today: {quota['remaining']} of {quota.get('limit') or '?'}")
    print(f"Latest CSV: {LATEST_CSV}")
    print(f"Historical snapshot: {csv_path}")

    return {
        "source": "pinnfeed",
        "events": len(events),
        "matches": len(matches),
        "api_seconds": fetch_seconds,
        "quota_remaining": quota.get("remaining") or None,
        "excluded": {r: rejected[r] for r in EXCLUDED_REASONS if rejected[r]},
        "invalid": {r: rejected[r] for r in INVALID_REASONS if rejected[r]},
    }


if __name__ == "__main__":
    main()
