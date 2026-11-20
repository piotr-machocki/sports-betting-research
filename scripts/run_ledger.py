"""One line per tennis run in data/runs/tennis.jsonl: whether each source worked.

run_once.py appends a record at the end of every run (normal or books-only), and the
dashboard's All tab (scripts/dashboard_runs.py) turns them into success rates. A
record is

    {"run_id", "kind": "session" | "books", "started_at", "finished_at", "status",
     "sources": {name: {"state": "ok" | "partial" | "failed" | "skipped", "detail"}}}

for the sources pinnacle, superbet, betclic, polymarket and kalshi. "partial" =
worked but lost something (Betclic: a competition skipped, or the feed fell back);
"skipped" = not tried (no key, a --no-* flag, a books-only run without Pinnacle).
Writing never stops a run: a failure only warns.

Runs before the ledger existed are rebuilt from the cron log:

    python scripts/run_ledger.py --backfill data/logs/scheduled_run.log.1.gz data/logs/scheduled_run.log

which adds a record (with "from_log": true) for every logged run that started before
the ledger's first one, and for every run cron skipped because the previous one was
still going (kind "skipped").
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER_FILE = ROOT / "data" / "runs" / "tennis.jsonl"
SESSIONS_DIR = ROOT / "data" / "results" / "sessions"
MARKETS_DIR = ROOT / "data" / "markets" / "tennis"

SOURCES = ["pinnacle", "betclic", "superbet", "polymarket", "kalshi"]
MARKET_SOURCES = ["polymarket", "kalshi"]
WORKED = {"ok", "partial"}


def state(name: str, detail: str = "") -> dict:
    return {"state": name, "detail": detail} if detail else {"state": name}


def utc_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ------------------------------------------------------------------ sources --


def betclic_state(performance: dict | None) -> dict:
    """From a session status file's performance block: completed with no phase error
    = ok, completed with one (a competition skipped, a feed fallback) = partial."""
    betclic = (performance or {}).get("betclic") or {}
    outcome = betclic.get("outcome") or ""

    if outcome != "completed":
        return state("failed", outcome or "no outcome")

    errors = [phase["error"] for phase in betclic.get("phases") or [] if phase.get("error")]
    names = [phase.get("name") for phase in betclic.get("phases") or []]

    if len(names) > 1 and names[0] == "feed":
        return state("partial", f"feed fell back to {', '.join(map(str, names[1:]))}")
    if errors:
        return state("partial", errors[0][:200])

    return state("ok")


def market_states(run_id: str, tried: bool) -> dict:
    """Polymarket and Kalshi from the run's saved snapshot: a source with rows worked.
    tennis_markets.py saves nothing when neither has rows."""
    if not tried:
        return {name: state("skipped") for name in MARKET_SOURCES}

    counts = dict.fromkeys(MARKET_SOURCES, 0)

    for path in sorted(MARKETS_DIR.glob(f"*/{run_id}.csv.gz"))[-1:]:
        try:
            with gzip.open(path, "rt", encoding="utf-8", newline="") as file:
                for row in csv.DictReader(file):
                    if row.get("source") in counts:
                        counts[row["source"]] += 1
        except (OSError, EOFError, csv.Error):
            pass

    return {
        name: state("ok") if count else state("failed", "no rows saved")
        for name, count in counts.items()
    }


# ------------------------------------------------------------------- writing --


def record(
    run_id: str,
    kind: str,
    started_at: str,
    status: str,
    sources: dict[str, dict],
    ledger: Path | None = None,
) -> None:
    """Appends one run; never raises."""
    ledger = ledger or LEDGER_FILE
    line = {
        "run_id": run_id,
        "kind": kind,
        "started_at": started_at,
        "finished_at": utc_text(datetime.now(timezone.utc)),
        "status": status,
        "sources": {name: sources.get(name, state("skipped")) for name in SOURCES},
    }

    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as file:
            file.write(json.dumps(line) + "\n")
    except OSError as exc:
        print(f"WARNING: run ledger not written: {exc}", flush=True)


def read(ledger: Path | None = None) -> list[dict]:
    """Every record, oldest first; unreadable lines are left out."""
    ledger = ledger or LEDGER_FILE
    try:
        text = ledger.read_text(encoding="utf-8")
    except OSError:
        return []

    out = []

    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue

    return sorted(out, key=lambda each: each.get("started_at") or "")


# ------------------------------------------------------------------ backfill --

HEADER = re.compile(r"^===== (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) UTC(: previous run still going, skipped)? =====$")
SESSION_LINE = re.compile(r"^Session ID:\s+(session_\S+)")
BOOKS_LINE = re.compile(r"^Run ID: (books_\S+)")
MARKET_OK = re.compile(r"^(polymarket|kalshi): \d+ matches")
MARKET_FAILED = re.compile(r"^WARNING: (polymarket|kalshi) failed: (.*)")


def log_lines(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open

    with opener(path, "rt", encoding="utf-8", errors="replace") as file:
        for line in file:
            yield line.rstrip("\n")


def log_blocks(paths: list[Path]):
    """(start time, lines, skipped) for each run in the cron logs, in order."""
    start, lines, skipped = None, [], False

    for path in paths:
        for line in log_lines(path):
            match = HEADER.match(line)

            if match:
                if start is not None:
                    yield start, lines, skipped
                start = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                lines, skipped = [], bool(match[2])
            elif start is not None:
                lines.append(line)

    if start is not None:
        yield start, lines, skipped


def status_performance(session_id: str) -> tuple[str, dict | None]:
    try:
        data = json.loads((SESSIONS_DIR / f"{session_id}_status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", None

    return data.get("status") or "", data.get("performance")


def parse_block(start: datetime, lines: list[str], skipped: bool) -> dict:
    """A ledger record from one run's log lines (and its session status file)."""
    base = {"started_at": utc_text(start), "finished_at": None, "from_log": True}

    if skipped:
        return {**base, "run_id": "", "kind": "skipped", "status": "skipped",
                "sources": {name: state("skipped") for name in SOURCES}}

    text = "\n".join(lines)
    run_id, kind = "", "session"

    for line in lines:
        if match := SESSION_LINE.match(line):
            run_id = match[1]
            break
        if match := BOOKS_LINE.match(line):
            run_id, kind = match[1], "books"
            break

    sources: dict[str, dict] = {}

    if "--- Pinnacle finished" in text:
        sources["pinnacle"] = state("ok")
    elif match := re.search(r"^ERROR: Pinnacle collection failed: (.*)$", text, re.M):
        sources["pinnacle"] = state("failed", match[1][:200])
    elif "Pinnacle skipped:" in text:
        sources["pinnacle"] = state("skipped", "no key")
    else:
        sources["pinnacle"] = state("failed", "no Pinnacle result in the log")

    if re.search(r"^Superbet: \d+ events", text, re.M):
        sources["superbet"] = state("ok")
    elif match := re.search(r"^WARNING: Superbet collection failed: (.*)$", text, re.M):
        sources["superbet"] = state("failed", match[1][:200])
    else:
        sources["superbet"] = state("skipped")

    status = ""

    if kind == "session" and run_id:
        status, performance = status_performance(run_id)
        sources["betclic"] = (
            betclic_state(performance) if performance else state("failed", "no status file")
        )
    elif kind == "books":
        if match := re.search(r"^WARNING: Betclic collection failed: (.*)$", text, re.M):
            sources["betclic"] = state("failed", match[1][:200])
        elif re.search(r"^Books saved: .*Betclic", text, re.M):
            sources["betclic"] = state("ok")
        else:
            sources["betclic"] = state("skipped")

    for line in lines:
        if match := MARKET_OK.match(line):
            sources[match[1]] = state("ok")
        elif match := MARKET_FAILED.match(line):
            sources[match[1]] = state("failed", match[2][:200])

    exit_code = re.findall(r"^exit code (\d+)$", text, re.M)

    if not status:
        status = "finished" if exit_code and exit_code[-1] == "0" else "failed"

    return {
        **base,
        "run_id": run_id,
        "kind": kind if run_id else "failed",
        "status": status,
        "sources": {name: sources.get(name, state("skipped")) for name in SOURCES},
    }


def backfill(paths: list[Path], ledger: Path | None = None) -> int:
    """Adds the logged runs that started before the ledger's first record; returns how
    many. The file is rewritten in time order."""
    ledger = ledger or LEDGER_FILE
    existing = read(ledger)
    first = min((each["started_at"] for each in existing if each.get("started_at")), default=None)
    known = {each["run_id"] for each in existing if each.get("run_id")}
    added = []

    for start, lines, skipped in log_blocks(paths):
        item = parse_block(start, lines, skipped)

        if first is not None and item["started_at"] >= first:
            continue
        if item["run_id"] and item["run_id"] in known:
            continue

        added.append(item)

    if not added:
        return 0

    every = sorted(existing + added, key=lambda each: each.get("started_at") or "")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    temporary = ledger.with_suffix(".tmp")
    temporary.write_text("".join(json.dumps(each) + "\n" for each in every), encoding="utf-8")
    os.replace(temporary, ledger)

    return len(added)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--backfill", nargs="+", type=Path, metavar="LOG",
                        help="cron logs, oldest first (.gz allowed)")
    args = parser.parse_args()

    if not args.backfill:
        parser.print_help()
        return 1

    print(f"Added {backfill(args.backfill)} runs to {LEDGER_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
