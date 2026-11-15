"""Read the bookmaker prices of a run's Telegram alerts again, about 2 minutes later.

Why (user, 16 Nov 2026): an alert sent at 00:15 had a lower price by 00:16, and with
runs 5 minutes apart a signal gone within 2 minutes looks the same as one that lasted
4. This measures it, for the alerted bets only:

    python scripts/recheck_signals.py [--session-id ID] [--after 150] [--no-wait]

scheduled_run.sh calls it after every run. With no alert sent in the run (the newest
session by default) it does nothing and makes no request. Otherwise it waits until
--after seconds from the run's start (the alerts go out about 30 s in), then runs
the collectors of the alerted books on just those matches:

* Betclic: src/betting/books/betclic_feed.py --no-queue --save (1-3 requests),
* Superbet: src/betting/books/superbet.py (one request),

and prices them against the same Pinnacle snapshot as the alert: no PinnAPI request,
since Pinnacle rarely moves in 2 minutes and the bookmaker's price is what goes. One
line per alert is appended to data/rechecks/rechecks.jsonl; the dashboard's Lasts
reads it. No retry, and a failure only prints a warning.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import notify  # noqa: E402
from betting.margin import remove_margin  # noqa: E402

PINNACLE_RAW_DIR = ROOT / "data" / "pinnacle" / "raw"
SUPERBET_RAW_DIR = ROOT / "data" / "superbet" / "raw"
RECHECK_DIR = ROOT / "data" / "rechecks"
RECHECKS_FILE = RECHECK_DIR / "rechecks.jsonl"
BETCLIC_FEED_SCRIPT = ROOT / "src" / "betting" / "books" / "betclic_feed.py"
SUPERBET_SCRIPT = ROOT / "src" / "betting" / "books" / "superbet.py"

AFTER_SECONDS = 150  # from the run's start: about 2 minutes after its alerts
ALERT_WINDOW_SECONDS = 300  # alerts sent this long after the run's start are its own
COLLECTOR_TIMEOUT = 90


def session_start(session_id: str) -> datetime | None:
    try:
        return datetime.strptime(session_id[8:23], "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def run_alerts(session_id: str, sent: dict) -> list[dict]:
    """The alerts sent by that run: entries of notify's sent memory first sent within
    ALERT_WINDOW_SECONDS of its start."""
    start = session_start(session_id)
    alerts = []

    if start is None:
        return alerts

    for alert in notify.sent_alerts_from(sent):
        at = alert.get("at")
        if at and 0 <= (at - start).total_seconds() <= ALERT_WINDOW_SECONDS:
            alerts.append(alert)

    return alerts


def number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pair(row: dict, prefix: str = "") -> tuple[str, str, str]:
    return row[f"{prefix}player1"], row[f"{prefix}player2"], row[f"{prefix}tournament"]


def fair_probability(row: dict, side: int) -> float | None:
    """Pinnacle's power-devigged probability for the side, as the alert used."""
    odds = [number(row.get("pinnacle_odds1") or row.get("odds1")), number(row.get("pinnacle_odds2") or row.get("odds2"))]

    if None in odds or min(odds) <= 1:
        return None

    return remove_margin(odds, "power")[side - 1]


def read_csv(path: Path) -> list[dict]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as file:
            return list(csv.DictReader(file))
    except OSError:
        return []


def collect(book: str, recheck_id: str, pinnacle_csv: Path, out_dir: Path) -> list[dict]:
    """Rows of the book's collector for the alerted matches only; [] on failure."""
    if book == "Betclic":
        script, saved = BETCLIC_FEED_SCRIPT, out_dir / "betclic.csv"
        extra = ["--no-queue", "--save", str(saved)]
    else:
        script, saved = SUPERBET_SCRIPT, SUPERBET_RAW_DIR / f"superbet_{recheck_id}.csv"
        extra = []

    if not script.exists():
        print(f"WARNING: recheck: {script.name} not found, {book} skipped", flush=True)
        return []

    command = [sys.executable, str(script), "--session-id", recheck_id, "--input", str(pinnacle_csv), *extra]

    try:
        subprocess.run(command, check=True, timeout=COLLECTOR_TIMEOUT, capture_output=True)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"WARNING: recheck: {book} collection failed: {type(exc).__name__}", flush=True)
        return []

    return read_csv(saved)


def price_now(book: str, row: dict | None, side: int) -> tuple[float | None, float | None]:
    """(price as the book shows it, EV) now, or (None, None) when the book no longer
    offers it prematch."""
    if row is None:
        return None, None

    if book == "Betclic":
        if row.get("status") != "FOUND" or row.get("betclic_status") == "live" or row.get("odds_ok") not in ("True", True):
            return None, None
        price = number(row.get(f"betclic_odds_for_pin_p{side}"))
        probability = fair_probability(row, side)
        ev = price * probability - 1 if price and probability else None
        return price, ev

    if row.get("superbet_status") != "FOUND":
        return None, None

    return number(row.get(f"superbet_price{side}")), number(row.get(f"superbet_ev{side}"))


def recheck(session_id: str, after: float, wait: bool = True) -> list[dict]:
    alerts = run_alerts(session_id, notify.load_sent())

    if not alerts:
        return []

    start = session_start(session_id)
    delay = start.timestamp() + after - time.time()

    if wait and delay > 0:
        time.sleep(delay)

    snapshot = read_csv(PINNACLE_RAW_DIR / f"pinnacle_{session_id}.csv")
    wanted = {pair(alert) for alert in alerts}
    rows = [row for row in snapshot if pair(row) in wanted]

    if not rows:
        print(f"WARNING: recheck: the alerted matches are not in {session_id}'s Pinnacle snapshot", flush=True)
        return []

    recheck_id = f"recheck_{session_id[8:]}"
    out_dir = RECHECK_DIR / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    pinnacle_csv = out_dir / "pinnacle.csv"

    with pinnacle_csv.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(snapshot[0]))
        writer.writeheader()
        writer.writerows(rows)

    found = {}
    for book in sorted({alert["book"] for alert in alerts}):
        for row in collect(book, recheck_id, pinnacle_csv, out_dir):
            found[(book, pair(row, "pinnacle_"))] = row

    checked_at = datetime.now(timezone.utc)
    records = []

    for alert in alerts:
        price, ev = price_now(alert["book"], found.get((alert["book"], pair(alert))), alert["side"])
        records.append({
            "session_id": session_id,
            "checked_at": checked_at.isoformat(),
            "alert_at": alert["at"].isoformat(),
            "book": alert["book"],
            "side": alert["side"],
            "player1": alert["player1"],
            "player2": alert["player2"],
            "tournament": alert["tournament"],
            "alert_price": alert.get("price"),
            "alert_ev": alert.get("ev"),
            "price": price,
            "ev": ev,
        })

    RECHECK_DIR.mkdir(parents=True, exist_ok=True)
    with RECHECKS_FILE.open("a", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record) + "\n")

    return records


def describe(record: dict) -> str:
    player = record[f"player{record['side']}"]
    after = (
        datetime.fromisoformat(record["checked_at"]) - datetime.fromisoformat(record["alert_at"])
    ).total_seconds()
    then = f"{record['alert_price']} ({record['alert_ev']:+.1%})" if record.get("alert_ev") is not None else "?"
    now = f"{record['price']} ({record['ev']:+.1%})" if record.get("ev") is not None else "gone"

    return f"Recheck {after:.0f}s after the alert: {record['book']} {player} {then} -> {now}"


def load_rechecks(path: Path = RECHECKS_FILE) -> list[dict]:
    """Every recheck line, for the dashboard; bad lines are skipped."""
    records = []

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return records

    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:
            continue

    return records


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--session-id", help="Default: the newest session.")
    parser.add_argument("--after", type=float, default=AFTER_SECONDS, help="Seconds from the run's start.")
    parser.add_argument("--no-wait", action="store_true", help="Check now instead of waiting.")
    args = parser.parse_args(argv)

    session_id = args.session_id or notify.latest_session_id()

    if not session_id:
        return

    try:
        records = recheck(session_id, args.after, wait=not args.no_wait)
    except Exception as exc:  # noqa: BLE001 - a run never fails here
        print(f"WARNING: recheck failed: {type(exc).__name__}: {exc}", flush=True)
        return

    for record in records:
        print(describe(record), flush=True)


if __name__ == "__main__":
    main()
