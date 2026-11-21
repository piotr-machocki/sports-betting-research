"""
The dashboard's All tab, Pipeline health subtab: a GitHub-style calendar per sport, one
cell a day coloured by the share of runs that worked (since 20 Nov 2026, user's
request; the user replaced the first card layout with this, more to be added).

Tennis from scripts/run_ledger.py's data/runs/tennis.jsonl (one line per run, older runs
rebuilt from the cron log), NBA from scripts/nba_collect.py's data/nba/runs.jsonl. A run
worked when every source it tried worked ("partial", e.g. Betclic lost a competition,
counts as worked); a run cron skipped because the previous one was still going counts
as failed. Days are local days, weeks start on Monday.
"""

from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import run_ledger

ROOT = Path(__file__).resolve().parents[1]
NBA_RUNS_FILE = ROOT / "data" / "nba" / "runs.jsonl"
WEEKS = 53

# (lowest share of runs that worked, CSS class), best first: greens when (nearly) all
# worked, yellows when mixed, reds when most failed; the stronger the colour, the
# further from the middle.
LEVELS = [
    (1.0, "g3"),
    (0.95, "g2"),
    (0.9, "g1"),
    (0.75, "y1"),
    (0.5, "y2"),
    (0.25, "r1"),
    (0.0, "r2"),
]

CALENDAR_STYLE = """<style>
.cal {
    --empty: rgba(128, 128, 128, 0.16);
    --g1: #0e6b30; --g2: #1f9d55; --g3: #39d353;
    --y1: #d4a72c; --y2: #9e6a03;
    --r1: #b62324; --r2: #f85149;
    --muted: color-mix(in srgb, currentColor 62%, transparent);
}
.cal-sport { margin: 6px 0 22px; }
.cal-title { font-size: 17px; font-weight: 600; margin-bottom: 8px; }
.cal-title span { font-weight: 400; font-size: 14px; color: var(--muted); }
.cal-box {
    display: inline-block; max-width: 100%;
    border: 1px solid rgba(128, 128, 128, 0.3); border-radius: 8px; padding: 12px 14px 10px;
}
/* Scrolls sideways on phones, starting at the newest week. */
.cal-scroll { overflow-x: auto; direction: rtl; }
.cal-grid {
    direction: ltr; display: inline-grid; gap: 3px;
    grid-template-columns: 28px repeat(var(--weeks), 11px);
    grid-template-rows: 15px repeat(7, 11px);
    font-size: 11px; line-height: 11px; color: var(--muted);
}
.cal-month { grid-row: 1; white-space: nowrap; }
.cal-day { grid-column: 1; }
.cal-cell { width: 11px; height: 11px; border-radius: 2px; background: var(--empty); }
.cal-cell[title] { cursor: help; }
.cal-cell.g1 { background: var(--g1); } .cal-cell.g2 { background: var(--g2); }
.cal-cell.g3 { background: var(--g3); }
.cal-cell.y1 { background: var(--y1); } .cal-cell.y2 { background: var(--y2); }
.cal-cell.r1 { background: var(--r1); } .cal-cell.r2 { background: var(--r2); }
.cal-cell:hover { outline: 1px solid currentColor; }
.cal-legend {
    display: flex; justify-content: flex-end; align-items: center; gap: 3px;
    margin-top: 8px; font-size: 12px; color: var(--muted);
}
.cal-legend .cal-cell { display: inline-block; }
.cal-legend .label { margin: 0 4px; }
</style>"""


def parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def nba_runs(path: Path | None = None) -> list[dict]:
    """nba_collect's runs in the ledger's shape: a source with an error failed, one
    marked skipped (props only in the hourly props run) was not tried."""
    path = path or NBA_RUNS_FILE

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    out = []

    for line in lines:
        try:
            run = json.loads(line)
        except ValueError:
            continue

        sources = {}

        for name, result in (run.get("sources") or {}).items():
            if "error" in result:
                sources[name] = run_ledger.state("failed", str(result["error"])[:200])
            elif "skipped" in result:
                sources[name] = run_ledger.state("skipped")
            else:
                sources[name] = run_ledger.state("ok")

        out.append({"kind": "session", "started_at": run.get("captured_at"), "sources": sources})

    return out


def run_worked(run: dict) -> bool:
    if run.get("kind") in {"skipped", "failed"}:
        return False

    tried = [each for each in run["sources"].values() if each["state"] != "skipped"]
    return bool(tried) and all(each["state"] in run_ledger.WORKED for each in tried)


def daily(runs: list[dict]) -> dict[date, tuple[int, int]]:
    """{local day: (runs that worked, runs)}."""
    out: dict[date, list[int]] = {}

    for run in runs:
        start = parse_time(run.get("started_at"))

        if start is None:
            continue

        counts = out.setdefault(start.astimezone().date(), [0, 0])
        counts[0] += run_worked(run)
        counts[1] += 1

    return {day: (worked, total) for day, (worked, total) in out.items()}


def level(worked: int, total: int) -> str:
    share = worked / total
    return next(css for floor, css in LEVELS if share >= floor)


def share_text(worked: int, total: int) -> str:
    share = worked / total
    return "100%" if share == 1 else f"{min(share, 0.999):.1%}"


def calendar_html(days: dict[date, tuple[int, int]], today: date, weeks: int = WEEKS) -> str:
    """Columns are weeks (Monday first), the newest on the right ending with today."""
    first = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks - 1)
    cells, months, last_month = [], [], None

    for week in range(weeks):
        monday = first + timedelta(weeks=week)
        column = week + 2

        if monday.month != last_month and (monday.day <= 7 or week == 0):
            if week < weeks - 2:
                months.append(
                    f'<div class="cal-month" style="grid-column: {column}">{monday:%b}</div>'
                )
            last_month = monday.month

        for weekday in range(7):
            day = monday + timedelta(days=weekday)
            if day > today:
                continue

            place = f"grid-column: {column}; grid-row: {weekday + 2}"
            counts = days.get(day)
            label = f"{day.day} {day:%b %Y}"

            if counts:
                worked, total = counts
                title = f"{label}: {share_text(worked, total)} worked ({worked} of {total} runs)"
                cells.append(
                    f'<div class="cal-cell {level(worked, total)}" style="{place}" '
                    f'title="{html.escape(title)}"></div>'
                )
            else:
                cells.append(
                    f'<div class="cal-cell" style="{place}" title="{label}: no runs"></div>'
                )

    labels = "".join(
        f'<div class="cal-day" style="grid-row: {row + 2}">{name}</div>'
        for row, name in ((0, "Mon"), (2, "Wed"), (4, "Fri"))
    )

    return (
        f'<div class="cal-scroll"><div class="cal-grid" style="--weeks: {weeks}">'
        f"{''.join(months)}{labels}{''.join(cells)}</div></div>"
    )


LEGEND = (
    '<div class="cal-legend"><span class="label">Bad</span>'
    + "".join(f'<span class="cal-cell {css}"></span>' for css in ("r2", "r1", "y2", "y1", "g1", "g2", "g3"))
    + '<span class="label">Good</span></div>'
)


def sport_html(sport: str, runs: list[dict], today: date) -> str:
    days = daily(runs)
    since = today - timedelta(weeks=WEEKS)
    total = sum(count for day, (_, count) in days.items() if day > since)

    return (
        f'<div class="cal-sport"><div class="cal-title">{html.escape(sport)} '
        f"<span>{total:,} runs in the last year</span></div>"
        f'<div class="cal-box">{calendar_html(days, today)}{LEGEND}</div></div>'
    )


def render_health(st) -> None:
    today = datetime.now(timezone.utc).astimezone().date()
    parts = [
        sport_html(sport, runs, today)
        for sport, runs in (("Tennis", run_ledger.read()), ("Basketball", nba_runs()))
    ]
    st.html(CALENDAR_STYLE + f'<div class="cal">{"".join(parts)}</div>')
