"""
The dashboard's All tab, Pipeline health subtab: how often each run and each source
worked (since 20 Nov 2026, user's request).

Tennis from scripts/run_ledger.py's data/runs/tennis.jsonl (one line per run, older runs
rebuilt from the cron log), NBA from scripts/nba_collect.py's data/nba/runs.jsonl. A
source's rate counts only the runs that tried it; "partial" (Betclic lost a competition,
or the feed fell back) counts as worked and is named in the card. A run worked when
every source it tried worked; a run cron skipped because the previous one was still
going counts as failed.

Layout (user, 20 Nov 2026: "nicer than a table"): per sport a heading with a status
pill (every source's latest attempt worked, or which ones failed) and the last run's
age, one wide card for the whole run and a card per source. Each card has its success
rate for the chosen window, a status chip for its latest attempt and an uptime strip of
time buckets (hover one for its counts).
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import run_ledger

ROOT = Path(__file__).resolve().parents[1]
NBA_RUNS_FILE = ROOT / "data" / "nba" / "runs.jsonl"

# Window label: (length, bucket size) for the uptime strip.
WINDOWS = {
    "24 h": (timedelta(hours=24), timedelta(hours=1)),
    "7 days": (timedelta(days=7), timedelta(hours=6)),
    "30 days": (timedelta(days=30), timedelta(days=1)),
}
DEFAULT_WINDOW = "24 h"
WINDOW_KEY = "health_window"
# A source whose latest attempt is older than this shows "No recent run".
STALE_AFTER = {"Tennis": timedelta(minutes=20), "Basketball": timedelta(minutes=45)}

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
WHOLE = "Whole run"
# Bucket colours: every tried run worked = ok, at least half = warn, else bad.
WARN_SHARE = 0.5

HEALTH_STYLE = """<style>
.health {
    --ok: #1f9d55;
    --warn: #c98a00;
    --bad: #d64545;
    --idle: rgba(128, 128, 128, 0.22);
    --muted: color-mix(in srgb, currentColor 62%, transparent);
    --line: rgba(128, 128, 128, 0.25);
}
.health-intro { font-size: 13px; color: var(--muted); margin: 0 0 4px; }
.health-sport { margin: 18px 0 26px; }
.health-sport-head {
    display: flex; flex-wrap: wrap; align-items: center; gap: 6px 14px; margin-bottom: 12px;
}
.health-sport-name { font-size: 22px; font-weight: 600; line-height: 30px; }
.health-sport-meta { font-size: 13px; color: var(--muted); }
.health-pill {
    display: inline-flex; align-items: center; gap: 7px;
    font-size: 14px; line-height: 24px; font-weight: 600;
    padding: 0 11px; border-radius: 12px;
    color: var(--status); background-color: color-mix(in srgb, var(--status) 15%, transparent);
}
.health-pill::before, .health-chip::before {
    content: ""; width: 7px; height: 7px; border-radius: 50%; background-color: currentColor;
}
.health-grid {
    display: grid; gap: 12px;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
}
.health-card {
    border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px 10px;
    display: flex; flex-direction: column; gap: 6px; min-width: 0;
}
.health-card.whole { grid-column: 1 / -1; }
.health-card-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.health-name { font-weight: 600; font-size: 15px; }
.health-chip {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 12px; font-weight: 600; color: var(--status); white-space: nowrap;
}
.health-rate-row { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 10px; }
.health-rate { font-size: 28px; line-height: 34px; font-weight: 600; font-variant-numeric: tabular-nums; }
.health-card.whole .health-rate { font-size: 34px; line-height: 40px; }
.health-sub { font-size: 13px; color: var(--muted); font-variant-numeric: tabular-nums; }
.health-facts { display: flex; flex-wrap: wrap; gap: 4px 28px; margin-left: auto; }
.health-facts .value { font-size: 17px; line-height: 22px; font-weight: 600; font-variant-numeric: tabular-nums; }
.health-facts .label { font-size: 12px; line-height: 16px; color: var(--muted); }
.health-bars { display: flex; gap: 2px; height: 26px; margin-top: 4px; }
.health-card.whole .health-bars { height: 34px; }
.health-bar { flex: 1 1 0; min-width: 2px; border-radius: 2px; background-color: var(--idle); cursor: help; }
.health-bar.ok { background-color: var(--ok); }
.health-bar.warn { background-color: var(--warn); }
.health-bar.bad { background-color: var(--bad); }
.health-bar:hover { outline: 2px solid currentColor; outline-offset: 1px; }
.health-axis { display: flex; justify-content: space-between; font-size: 11px; color: var(--muted); }
.health-last {
    font-size: 12px; color: var(--muted); border-top: 1px solid var(--line);
    padding-top: 6px; margin-top: 2px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.health-last[title] { cursor: help; }
.health-legend { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 12px; color: var(--muted); margin-top: 4px; }
.health-legend span { display: inline-flex; align-items: center; gap: 6px; }
.health-legend i { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
/* Phones: source cards two a row and compact, so the page is not one long column. */
@media (max-width: 700px) {
    .health-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
    .health-card { padding: 10px 10px 8px; gap: 4px; }
    .health-card:not(.whole) .health-card-head { flex-direction: column; align-items: flex-start; gap: 0; }
    .health-card:not(.whole) .health-rate { font-size: 22px; line-height: 28px; }
    .health-card:not(.whole) .health-sub { font-size: 12px; width: 100%; }
    .health-card:not(.whole) .health-bars { height: 20px; gap: 1px; }
    .health-card:not(.whole) .health-axis { display: none; }
    .health-facts { margin-left: 0; width: 100%; gap: 4px 20px; }
    .health-last { font-size: 11px; }
}
</style>"""

STATUS_COLORS = {"ok": "var(--ok)", "partial": "var(--warn)", "failed": "var(--bad)", "stale": "#888888"}
STATUS_TEXT = {"ok": "Working", "partial": "Partial", "failed": "Failing", "stale": "No recent run"}


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


def timed(runs: list[dict]) -> list[tuple[datetime, dict]]:
    """(start, run) oldest first, runs without a readable start left out."""
    out = [(parse_time(run.get("started_at")), run) for run in runs]
    return sorted(((start, run) for start, run in out if start), key=lambda pair: pair[0])


def attempts(runs: list[tuple[datetime, dict]], name: str) -> list[tuple[datetime, dict]]:
    """(start, state) of every run that tried `name` (WHOLE = the run itself)."""
    if name == WHOLE:
        return [
            (start, run_ledger.state("ok") if run_worked(run)
             else run_ledger.state("failed", first_problem(run)))
            for start, run in runs
        ]

    out = []

    for start, run in runs:
        each = run["sources"].get(name)

        if run.get("kind") != "skipped" and each and each["state"] != "skipped":
            out.append((start, each))

    return out


def rate_stats(items: list[tuple[datetime, dict]]) -> dict:
    worked = [pair for pair in items if pair[1]["state"] in run_ledger.WORKED]
    failures = [pair for pair in items if pair[1]["state"] not in run_ledger.WORKED]

    return {
        "tried": len(items),
        "worked": len(worked),
        "partial": sum(each["state"] == "partial" for _, each in items),
        "last_failure": failures[-1] if failures else None,
    }


def buckets(
    items: list[tuple[datetime, dict]], now: datetime, length: timedelta, step: timedelta
) -> list[dict]:
    """Equal time buckets over the window, oldest first: start, end, tried, worked."""
    count = round(length / step)
    first = now - count * step
    out = [
        {"start": first + index * step, "end": first + (index + 1) * step, "tried": 0, "worked": 0}
        for index in range(count)
    ]

    for start, each in items:
        if start < first or start > now:
            continue
        bucket = out[min(int((start - first) / step), count - 1)]
        bucket["tried"] += 1
        bucket["worked"] += each["state"] in run_ledger.WORKED

    return out


def current_status(items: list[tuple[datetime, dict]], now: datetime, stale: timedelta) -> str:
    """ok / partial / failed from the latest attempt, or stale."""
    if not items or now - items[-1][0] > stale:
        return "stale"
    return items[-1][1]["state"] if items[-1][1]["state"] in STATUS_TEXT else "failed"


def health(runs: list[dict], sources: list[str], now: datetime, window: str, stale: timedelta) -> dict:
    """Everything the cards show for one sport and window."""
    length, step = WINDOWS[window]
    every = timed(runs)
    chosen = [(start, run) for start, run in every if start >= now - length]
    cards = []

    for name in [WHOLE, *sources]:
        all_items = attempts(every, name)
        items = [pair for pair in all_items if pair[0] >= now - length]

        if name != WHOLE and not items and not all_items:
            continue

        cards.append({
            "name": name,
            "stats": rate_stats(items),
            "buckets": buckets(items, now, length, step),
            "status": current_status(all_items, now, stale),
            # The newest failure ever, so a card that is green now still says when.
            "last_failure": rate_stats(all_items)["last_failure"],
        })

    starts = [start for start, _ in chosen]
    gaps = [later - earlier for earlier, later in zip(starts, starts[1:])]

    return {
        "cards": cards,
        "runs": len(chosen),
        "skipped": sum(run.get("kind") == "skipped" for _, run in chosen),
        "longest_gap": max(gaps) if gaps else None,
        "last_run": every[-1][0] if every else None,
        "first_run": every[0][0] if every else None,
    }


# ----------------------------------------------------------------- drawing --


def local_time(moment: datetime) -> str:
    local = moment.astimezone()
    return f"{local.day} {local:%b %H:%M}"


def age_text(gap: timedelta | None) -> str:
    if gap is None:
        return "--"
    minutes = gap.total_seconds() / 60
    if minutes < 1:
        return "under 1 min"
    if minutes < 120:
        return f"{minutes:.0f} min"
    if minutes < 48 * 60:
        return f"{minutes / 60:.1f} h"
    return f"{minutes / 1440:.0f} days"


def rate_text(stats: dict) -> str:
    if not stats["tried"]:
        return "--"
    share = stats["worked"] / stats["tried"]
    return "100%" if share == 1 else f"{min(share, 0.999):.1%}"


def bar_html(bucket: dict, window: str) -> str:
    if window == "30 days":
        span = f"{bucket['start'].astimezone():%a %d %b}"
    else:
        span = f"{local_time(bucket['start'])}–{bucket['end'].astimezone():%H:%M}"

    if not bucket["tried"]:
        css, text = "", "no runs"
    else:
        share = bucket["worked"] / bucket["tried"]
        css = "ok" if share == 1 else "warn" if share >= WARN_SHARE else "bad"
        text = f"{bucket['worked']} of {bucket['tried']} worked"

    return f'<div class="health-bar {css}" title="{html.escape(f"{span}: {text}")}"></div>'


def chip_html(status: str) -> str:
    return (
        f'<span class="health-chip" style="--status: {STATUS_COLORS[status]}">'
        f"{STATUS_TEXT[status]}</span>"
    )


def card_html(card: dict, labels: dict[str, str], window: str, facts: str = "") -> str:
    stats = card["stats"]
    whole = card["name"] == WHOLE
    label = WHOLE if whole else labels.get(card["name"], card["name"])

    if stats["tried"]:
        sub = f"{stats['worked']:,} of {stats['tried']:,} runs worked"
        if stats["partial"]:
            sub += f" · {stats['partial']:,} partly"
    else:
        sub = "not tried in this window"

    failure = card["last_failure"]
    if failure is None:
        last = '<div class="health-last">No failure recorded</div>'
    else:
        start, each = failure
        detail = each.get("detail") or "failed"
        last = (
            f'<div class="health-last" title="{html.escape(detail)}">Last failure '
            f"{local_time(start)} · {html.escape(detail)}</div>"
        )

    return (
        f'<div class="health-card{" whole" if whole else ""}">'
        f'<div class="health-card-head"><span class="health-name">{html.escape(label)}</span>'
        f"{chip_html(card['status'])}</div>"
        f'<div class="health-rate-row"><span class="health-rate">{rate_text(stats)}</span>'
        f'<span class="health-sub">{sub}</span>{facts}</div>'
        f'<div class="health-bars">{"".join(bar_html(each, window) for each in card["buckets"])}</div>'
        f'<div class="health-axis"><span>{window} ago</span><span>now</span></div>'
        f"{last}</div>"
    )


def facts_html(result: dict) -> str:
    facts = [
        (f"{result['runs']:,}", "Runs"),
        (age_text(result["longest_gap"]), "Longest gap"),
        (f"{result['skipped']:,}", "Skipped by cron"),
    ]
    return '<div class="health-facts">' + "".join(
        f'<div><div class="value">{value}</div><div class="label">{label}</div></div>'
        for value, label in facts
    ) + "</div>"


def sport_html(sport: str, result: dict, labels: dict[str, str], window: str, now: datetime) -> str:
    sources = [card for card in result["cards"] if card["name"] != WHOLE]
    failing = [labels.get(card["name"], card["name"]) for card in sources if card["status"] == "failed"]
    partial = [labels.get(card["name"], card["name"]) for card in sources if card["status"] == "partial"]
    stale = result["last_run"] is None or all(card["status"] == "stale" for card in sources)

    if stale:
        pill, color = "No recent run", STATUS_COLORS["stale"]
    elif failing:
        pill, color = "Failing: " + ", ".join(failing), STATUS_COLORS["failed"]
    elif partial:
        pill, color = "Partial: " + ", ".join(partial), STATUS_COLORS["partial"]
    else:
        pill, color = "All sources working", STATUS_COLORS["ok"]

    meta = []
    if result["last_run"]:
        meta.append(f"Last run {age_text(now - result['last_run'])} ago")
    if result["first_run"]:
        meta.append(f"recorded since {local_time(result['first_run'])}")

    whole = next(card for card in result["cards"] if card["name"] == WHOLE)
    cards = [card_html(whole, labels, window, facts_html(result))]
    cards += [card_html(card, labels, window) for card in sources]

    return (
        '<div class="health-sport">'
        f'<div class="health-sport-head"><span class="health-sport-name">{html.escape(sport)}</span>'
        f'<span class="health-pill" style="--status: {color}">{html.escape(pill)}</span>'
        f'<span class="health-sport-meta">{" · ".join(meta)}</span></div>'
        f'<div class="health-grid">{"".join(cards)}</div></div>'
    )


LEGEND = (
    '<div class="health-legend">'
    '<span><i style="background: var(--ok)"></i>every run worked</span>'
    '<span><i style="background: var(--warn)"></i>some failed</span>'
    '<span><i style="background: var(--bad)"></i>most failed</span>'
    '<span><i style="background: var(--idle)"></i>no runs</span>'
    "</div>"
)


def sport_sources(runs: list[dict], labels: dict[str, str]) -> list[str]:
    names = {name for run in runs for name in run["sources"]}
    return [name for name in labels if name in names] + sorted(names - set(labels))


def render_health(st) -> None:
    now = datetime.now(timezone.utc)

    with st.container(horizontal=True, vertical_alignment="center"):
        window = st.segmented_control(
            "Window", list(WINDOWS), default=DEFAULT_WINDOW, key=WINDOW_KEY,
            label_visibility="collapsed",
        ) or DEFAULT_WINDOW

    parts = [
        '<div class="health-intro">How often each run and each source worked. A source '
        "counts only the runs that tried it; a run worked when every source it tried did. "
        "Hover a bar for its counts, a failure for its full message.</div>",
        LEGEND,
    ]

    for sport, runs, labels in (
        ("Tennis", run_ledger.read(), TENNIS_LABELS),
        ("Basketball", nba_runs(), NBA_LABELS),
    ):
        if not runs:
            parts.append(
                f'<div class="health-sport"><div class="health-sport-name">{sport}</div>'
                '<div class="health-intro">No runs recorded yet.</div></div>'
            )
            continue

        result = health(runs, sport_sources(runs, labels), now, window, STALE_AFTER[sport])
        parts.append(sport_html(sport, result, labels, window, now))

    st.html(HEALTH_STYLE + f'<div class="health">{"".join(parts)}</div>')
