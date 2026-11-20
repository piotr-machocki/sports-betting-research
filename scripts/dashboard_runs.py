"""
The dashboard's All tab: how often each run and each source worked (since 20 Nov 2026,
user's request).

Tennis from scripts/run_ledger.py's data/runs/tennis.jsonl (one line per run, older runs
rebuilt from the cron log), NBA from scripts/nba_collect.py's data/nba/runs.jsonl. A
source's rate counts only the runs that tried it; "partial" (Betclic lost a competition,
or the feed fell back) counts as worked and is named in the tooltip. A run worked when
every source it tried worked; a run cron skipped because the previous one was still
going counts as failed.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import run_ledger

ROOT = Path(__file__).resolve().parents[1]
NBA_RUNS_FILE = ROOT / "data" / "nba" / "runs.jsonl"

WINDOWS = [("Last 24 h", timedelta(hours=24)), ("Last 7 days", timedelta(days=7)), ("All", None)]
TENNIS_LABELS = {
    "pinnacle": "Pinnacle",
    "betclic": "Betclic",
    "superbet": "Superbet",
    "polymarket": "Polymarket",
    "kalshi": "Kalshi",
}
NBA_LABELS = {
    "pinnacle": "Pinnacle",
    "betclic": "Betclic",
    "superbet": "Superbet",
    "actionnetwork": "Action Network",
    "espn": "ESPN",
    "espn_props": "ESPN props",
    "scoresandodds": "scoresandodds",
    "kalshi": "Kalshi",
    "polymarket": "Polymarket",
}
GOOD, FAIR = 0.99, 0.90

RUNS_STYLE = """<style>
.runs-sport { font-size: 18px; font-weight: 600; margin: 4px 0 2px; }
.runs-note { font-size: 13px; color: color-mix(in srgb, currentColor 65%, transparent); margin-bottom: 8px; }
table.runs { border-collapse: collapse; font-size: 14px; margin-bottom: 18px; }
table.runs th, table.runs td {
    padding: 5px 12px;
    border-bottom: 1px solid rgba(128, 128, 128, 0.25);
    text-align: right;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
}
table.runs th { font-weight: 400; color: color-mix(in srgb, currentColor 65%, transparent); }
table.runs th:first-child, table.runs td:first-child { text-align: left; }
table.runs td.last { text-align: left; white-space: normal; max-width: 360px;
    color: color-mix(in srgb, currentColor 70%, transparent); font-size: 13px; }
table.runs tr.total td { font-weight: 600; }
table.runs .rate-good { color: #1f9d55; }
table.runs .rate-fair { color: #c98a00; }
table.runs .rate-bad { color: #d64545; }
table.runs .none { color: #888888; }
table.runs td[title] { cursor: help; }
.runs-wrap { overflow-x: auto; }
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

        out.append({
            "run_id": run.get("run_id", ""),
            "kind": "session",
            "started_at": run.get("captured_at"),
            "sources": sources,
        })

    return out


def run_worked(run: dict) -> bool:
    if run.get("kind") in {"skipped", "failed"}:
        return False

    tried = [each for each in run["sources"].values() if each["state"] != "skipped"]
    return bool(tried) and all(each["state"] in run_ledger.WORKED for each in tried)


def first_problem(run: dict) -> str:
    if run.get("kind") == "skipped":
        return "skipped by cron: the previous run was still going"

    for name, each in run["sources"].items():
        if each["state"] == "failed":
            return f"{name}: {each.get('detail') or 'failed'}"

    return "no source worked"


def rate_stats(items: list[tuple[datetime, dict | None]]) -> dict:
    """items = (start, state or None for 'did the whole run work'); counts tried runs."""
    tried = [(start, each) for start, each in items if each["state"] != "skipped"]
    worked = [pair for pair in tried if pair[1]["state"] in run_ledger.WORKED]
    partial = sum(each["state"] == "partial" for _, each in tried)
    failures = [pair for pair in tried if pair[1]["state"] not in run_ledger.WORKED]

    return {
        "tried": len(tried),
        "worked": len(worked),
        "partial": partial,
        "last_failure": failures[-1] if failures else None,
    }


def window_runs(runs: list[dict], since: datetime | None) -> list[tuple[datetime, dict]]:
    out = []

    for run in runs:
        start = parse_time(run.get("started_at"))

        if start is not None and (since is None or start >= since):
            out.append((start, run))

    return out


def health(runs: list[dict], sources: list[str], now: datetime) -> dict:
    """{row label: [stats per window]} for 'Whole run' and each source, plus each
    window's run count and longest gap between runs."""
    table: dict[str, list[dict]] = {"Whole run": [], **{name: [] for name in sources}}
    meta = []

    for _, length in WINDOWS:
        chosen = window_runs(runs, now - length if length else None)
        whole = [
            (start, run_ledger.state(
                "ok" if run_worked(run) else "failed",
                "" if run_worked(run) else first_problem(run),
            ))
            for start, run in chosen
        ]
        table["Whole run"].append(rate_stats(whole))

        for name in sources:
            table[name].append(rate_stats([
                (start, run["sources"].get(name, run_ledger.state("skipped")))
                for start, run in chosen
                if run.get("kind") != "skipped"
            ]))

        starts = sorted(start for start, _ in chosen)
        gaps = [later - earlier for earlier, later in zip(starts, starts[1:])]
        meta.append({
            "runs": len(starts),
            "first": starts[0] if starts else None,
            "longest_gap": max(gaps) if gaps else None,
        })

    return {"rows": table, "meta": meta}


def local_time(moment: datetime) -> str:
    local = moment.astimezone()
    return f"{local.day} {local:%b %H:%M}"


def gap_text(gap: timedelta | None) -> str:
    if gap is None:
        return "--"
    minutes = gap.total_seconds() / 60
    return f"{minutes:.0f} min" if minutes < 120 else f"{minutes / 60:.1f} h"


def rate_cell(stats: dict) -> str:
    if not stats["tried"]:
        return '<td class="none" title="Not tried in this window">--</td>'

    share = stats["worked"] / stats["tried"]
    css = "rate-good" if share >= GOOD else "rate-fair" if share >= FAIR else "rate-bad"
    title = f"{stats['worked']} of {stats['tried']} runs worked"
    if stats["partial"]:
        title += f", {stats['partial']} of them partly (something lost, see the detail)"
    text = f"{share:.1%}" if share < 1 else "100%"

    return f'<td class="{css}" title="{html.escape(title)}">{text}</td>'


def last_cell(stats: dict) -> str:
    failure = stats["last_failure"]

    if failure is None:
        return '<td class="last">none</td>'

    start, each = failure
    detail = each.get("detail") or "failed"
    short = detail if len(detail) <= 70 else detail[:67] + "..."

    return (
        f'<td class="last" title="{html.escape(detail)}">'
        f"{local_time(start)} · {html.escape(short)}</td>"
    )


def health_table(result: dict, labels: dict[str, str]) -> str:
    heads = "".join(f"<th>{label}</th>" for label, _ in WINDOWS)
    rows = []

    for name, stats in result["rows"].items():
        if name != "Whole run" and not any(each["tried"] for each in stats):
            continue
        label = "Whole run" if name == "Whole run" else labels.get(name, name)
        rows.append(
            f'<tr class="{"total" if name == "Whole run" else ""}">'
            f"<td>{html.escape(label)}</td>"
            + "".join(rate_cell(each) for each in stats)
            # The newest failure in any window is in the widest one.
            + last_cell(stats[-1])
            + "</tr>"
        )

    meta = result["meta"]
    rows.append(
        "<tr><td>Runs</td>"
        + "".join(f"<td>{each['runs']:,}</td>" for each in meta)
        + "<td></td></tr>"
    )
    rows.append(
        '<tr><td title="Longest time between two runs: the VM or cron being down shows here">'
        "Longest gap</td>"
        + "".join(f"<td>{gap_text(each['longest_gap'])}</td>" for each in meta)
        + "<td></td></tr>"
    )

    return (
        '<div class="runs-wrap"><table class="runs"><thead><tr><th></th>'
        f"{heads}<th style=\"text-align:left\">Last failure</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def since_note(result: dict) -> str:
    first = result["meta"][-1]["first"]
    return f"Since {local_time(first)}." if first else ""


def render_runs(st) -> None:
    now = datetime.now(timezone.utc)
    st.html(RUNS_STYLE)
    st.html(
        '<div class="runs-note">How often each run and each source worked. A source '
        "counts only the runs that tried it; a run worked when every source it tried "
        "did. Hover a rate for the counts, a failure for its full message.</div>"
    )

    for sport, runs, labels in (
        ("Tennis", run_ledger.read(), TENNIS_LABELS),
        ("Basketball (NBA price log)", nba_runs(), NBA_LABELS),
    ):
        if not runs:
            st.html(f'<div class="runs-sport">{sport}</div><div class="runs-note">No runs recorded yet.</div>')
            continue

        sources = [name for name in labels if any(name in run["sources"] for run in runs)]
        sources += sorted({name for run in runs for name in run["sources"]} - set(sources))
        result = health(runs, sources, now)
        st.html(
            f'<div class="runs-sport">{sport}</div>'
            f'<div class="runs-note">{since_note(result)}</div>'
            + health_table(result, labels)
        )
