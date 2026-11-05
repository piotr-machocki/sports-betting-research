"""
Local web view of the session files: filters, no width limit.

    python scripts/dashboard.py    # or: python -m streamlit run scripts/dashboard.py

It only reads what live_monitor.py reads (data/results/sessions/ and the
optional Superbet CSV) and writes nothing, so it can run beside the pipeline,
before it or without it. "Latest" follows the newest session: a new run_once.py
run appears by itself, and its matches appear while the worker writes them.

The header ticks every second during a run and every 10 s otherwise, because each
tick stalls the browser enough to make scrolling stutter. The table is rebuilt only
when the session file or a filter changes: styling and drawing it is the slow part.

The server stops by itself a few seconds after the last browser tab closes, and
waits as long as it takes for the first one. A page reload does not stop it.

Each table row is one match (latest observation), for the BET ON side, as in
the terminal table. It is a plain HTML table, not st.dataframe: that one draws on a
canvas as tall as the table and repaints it while scrolling. The page's own
scrollbars move this one, so the browser scrolls it without repainting anything.
"""

from __future__ import annotations

import html
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(Path(__file__).resolve().parent))

from live_monitor import (  # noqa: E402
    EV_LABELS,
    FINISHED_STATES,
    SESSIONS_DIR,
    STUCK_AFTER_SECONDS,
    fmt_age,
    format_ev,
    format_odds,
    format_probability,
    has_superbet,
    NO_EV_LABEL,
    latest_per_match,
    no_ev_reasons,
    number,
    parse_timestamp,
    pinnacle_info,
    read_runner_info,
    read_session_rows,
    sort_key,
    stats_items,
    table_side,
)
from results_store import METHODS, REFERENCE_METHOD, ev_column, overround  # noqa: E402

REFRESH_SECONDS = 1
IDLE_REFRESH_SECONDS = 10

# Long enough for a page reload, which drops the connection and opens a new one.
STOP_AFTER_SECONDS = 10

# live_monitor's defaults for --min-ev and --max-age.
DEFAULT_MIN_EV_PERCENT = 2.0
STALE_MINUTES = 15.0

# Markdown strips leading spaces from a line, but not this character.
SUBGROUP_INDENT = " "

LATEST = "Latest (follows new runs)"

# Most specific first: "ATP Challenger X" also starts with "ATP".
TOUR_PREFIXES = {
    "atp challenger": "Challenger",
    "wta 125k": "WTA 125",
    "itf men": "ITF Men",
    "itf women": "ITF Women",
    "atp": "ATP",
    "wta": "WTA",
}
OTHER_TOUR = "Other"
TOURS = ["ATP", "WTA", "Challenger", "WTA 125", "ITF Men", "ITF Women", OTHER_TOUR]
DRAWS = ["Main", "Qualifiers", "Doubles"]

EV_ALL = "All matches"
EV_CALCULATED = "With EV"
EV_POSITIVE = "EV > 0"
EV_THRESHOLD = "EV >= threshold"
EV_FILTERS = [EV_ALL, EV_CALCULATED, EV_POSITIVE, EV_THRESHOLD]

NAME_COLUMNS = {"Bet on", "Opponent"}

TEXT_COLUMNS = {
    *NAME_COLUMNS,
    "Tournament",
    "Match conf",
}

# Only for the filters: the tournament text says Tour and Draw, and the EV
# columns show Best EV.
FILTER_COLUMNS = {"Best EV", "Tour", "Draw"}

OTHER_EV_COLUMNS = {
    method: EV_LABELS[method] for method in METHODS if method != REFERENCE_METHOD
}

# One of each pair shows at a time; the switch in their headers changes which.
TAX_SWITCH_CLASSES = {
    "Superbet": "superbet-shown",
    "Superbet net": "superbet-net",
    "Margin Superbet": "margin-shown",
    "Margin Superbet net": "margin-net",
}
TAX_SWITCH = (
    '<span class="tax-switch" title="Switch between before and after tax"></span>'
)

# The other devig methods' EV, for one book at a time: every pair follows one switch,
# which sits in the first pair's headers, so one shows whichever book is on screen.
EV_BOOK_CLASSES = {
    f"{label} {book}": f"ev-{book.lower()}"
    for label in OTHER_EV_COLUMNS.values()
    for book in ("Betclic", "Superbet")
}
BOOK_SWITCH_COLUMNS = list(EV_BOOK_CLASSES)[:2]
BOOK_SWITCH = (
    '<span class="book-switch" '
    'title="Switch all three of these EV columns between Betclic and Superbet"></span>'
)

SESSION_ID = re.compile(r"^session_(\d{8}_\d{6})_[0-9a-f]+$")

# Readable on both the light and the dark theme.
EV_RED = "color: #d64545"
EV_GRAY = "color: #888888"
EV_AMBER = "color: #c98a00"
EV_GREEN = "color: #1f9d55; background-color: rgba(31, 157, 85, 0.16)"
DIMMED = "color: #888888"
BET_ON = "color: #0e8fa8"

# Streamlit fades elements while a rerun is in progress. With the refresh timer
# that would make the page pulse whenever a refresh takes over 0.5 s.
NO_STALE_FADE = """<style>
[data-testid="stElementContainer"][data-stale="true"] {
    opacity: 1 !important;
    transition: none !important;
}
</style>"""

# The page scrolls both ways, so its scrollbars stay on screen and the headers and
# first column stick. Chrome ignores ::-webkit-scrollbar unless scrollbar-width and
# -color are auto; a thumb shows while the mouse is over the page, as Streamlit's do.
TABLE_STYLE = """<style>
[data-testid="stMain"] {
    scrollbar-width: auto;
    scrollbar-color: auto;
}
[data-testid="stMain"]::-webkit-scrollbar {
    width: 10px;
    height: 10px;
}
[data-testid="stMain"]::-webkit-scrollbar-corner {
    background: transparent;
}
[data-testid="stMain"]::-webkit-scrollbar-thumb {
    border-radius: 5px;
}
[data-testid="stMain"]:hover::-webkit-scrollbar-thumb {
    background: color-mix(in srgb, currentColor 40%, transparent);
}
table.matches {
    border-collapse: separate;
    border-spacing: 0;
    font-size: 14px;
    white-space: nowrap;
}
table.matches th, table.matches td {
    padding: 6px 10px;
    line-height: 22px;  /* whole pixels, or sticky cells hide some row lines */
    border-bottom: 1px solid rgba(128, 128, 128, 0.25);
    border-right: 1px solid rgba(128, 128, 128, 0.25);
    text-align: left;
    background-clip: padding-box;
}
table.matches th {
    position: sticky;
    top: 0;
    z-index: 2;
    font-weight: 400;
    color: color-mix(in srgb, currentColor 65%, transparent);
    background-color: var(--page-background);
}
table.matches th:first-child, table.matches td:first-child {
    position: sticky;
    left: 0;
    background-color: var(--page-background);
}
table.matches th:last-child, table.matches td:last-child { border-right: none; }
table.matches td:first-child { z-index: 1; }
table.matches th:first-child { z-index: 3; }
table.matches .number { text-align: right; font-variant-numeric: tabular-nums; }
table.matches .superbet-net, table.matches .margin-net { display: none; }
:root[data-superbet-price="net"] table.matches .superbet-net { display: table-cell; }
:root[data-superbet-price="net"] table.matches .superbet-shown { display: none; }
:root[data-superbet-margin="net"] table.matches .margin-net { display: table-cell; }
:root[data-superbet-margin="net"] table.matches .margin-shown { display: none; }
table.matches .ev-superbet { display: none; }
:root[data-ev-book="superbet"] table.matches .ev-superbet { display: table-cell; }
:root[data-ev-book="superbet"] table.matches .ev-betclic { display: none; }
table.matches td[data-flip] { cursor: pointer; }
table.matches td.tax-flipped { background-color: rgba(201, 138, 0, 0.18); }
table.matches .tax-switch, table.matches .book-switch {
    cursor: pointer;
    user-select: none;
    margin-left: 4px;
    padding: 0 4px;
    border-radius: 4px;
    outline: 1px solid rgba(128, 128, 128, 0.5);
}
table.matches .tax-switch:hover, table.matches .book-switch:hover {
    background-color: rgba(128, 128, 128, 0.3);
}
table.matches .tax-switch::before, table.matches .book-switch::before {
    content: "\\21C4";
}
</style>"""

# One horizontal row of cards; on a narrow window they wrap. Same colours in both themes:
# they hold up on light and dark, and every bar has its numbers printed beside it.
OVERVIEW_BLUE = "#2a78d6"
OVERVIEW_NO_EV = "#c98a00"
SUPERBET_NO_EV_LABEL = "FOUND on Superbet but no EV"
OVERVIEW_NOT_MATCHED = "rgba(128, 128, 128, 0.65)"
OVERVIEW_WAITING = "rgba(128, 128, 128, 0.2)"
OVERVIEW_STYLE = """<style>
.overview { margin-bottom: 4px; }
.overview-title { font-weight: 600; margin-bottom: 6px; }
.overview-cards { display: flex; flex-wrap: wrap; gap: 10px; }
.overview-cards + .overview-cards { margin-top: 10px; }
.overview-card {
    box-sizing: border-box;
    flex: 1 1 200px;
    min-width: 0;
    padding: 8px 12px;
    border: 1px solid rgba(128, 128, 128, 0.25);
    border-radius: 8px;
    font-size: 13px;
    line-height: 20px;
}
.overview-card.wide { flex: 2 1 220px; }
/* Holds the third column, so the second row lines up with the first; no height when it wraps. */
.overview-card.spacer { height: 0; padding: 0; border: 0; }
.overview-heading {
    margin-bottom: 4px;
    font-size: 12px;
    color: color-mix(in srgb, currentColor 65%, transparent);
}
.overview-meters {
    display: grid;
    grid-template-columns: auto minmax(40px, 1fr) auto;
    align-items: center;
    gap: 0 10px;
}
.overview-meters .count { text-align: right; font-variant-numeric: tabular-nums; }
.overview-meter {
    height: 8px;
    border-radius: 4px;
    overflow: hidden;
    background-color: rgba(42, 120, 214, 0.2);
}
.overview-meter > div { height: 100%; border-radius: 4px; }
.overview-split {
    display: flex;
    gap: 2px;
    height: 8px;
    margin: 6px 0;
    border-radius: 4px;
    overflow: hidden;
}
.overview-split > div { min-width: 4px; }
.overview-legend { display: flex; flex-wrap: wrap; gap: 0 14px; }
.overview-swatch {
    display: inline-block;
    width: 9px;
    height: 9px;
    margin-right: 5px;
    border-radius: 2px;
}
.overview-note { color: color-mix(in srgb, currentColor 65%, transparent); }
.overview-tiles { display: flex; flex-wrap: wrap; gap: 4px 18px; }
.overview-tile .value { font-size: 22px; line-height: 28px; font-weight: 600; }
.overview-tile .label {
    font-size: 12px;
    line-height: 16px;
    color: color-mix(in srgb, currentColor 65%, transparent);
}
</style>"""

# Installed once. Streamlit runs a script before inserting the HTML sent with it, and
# its sanitizer drops ids, so this watches for each new table by its class. Watching
# also keeps the opaque sticky cells in step with the theme. The Superbet price, margin
# and EV book choices sit on the root element, so new tables get them without any work here.
# A double-clicked price or margin swaps text with its hidden partner cell, so it shows
# the other value under either header. A redraw loses that, so flipped cells are kept
# by name and flipped again in each new table. Rows stay in the order they were sent.
TABLE_SCRIPT = """<script>
(() => {
    if (window.matchesTableReady) return;
    window.matchesTableReady = true;

    let seen = null;
    const MARGIN_CELLS = ".margin-shown, .margin-net";
    const CELL_PAIRS = [".superbet-shown, .superbet-net", MARGIN_CELLS];
    const flipped = new Set();
    const flipKey = (row, pair) =>
        `${pair}|${row.cells[0].textContent}|${row.cells[1].textContent}`;

    function flipCells(row, pair) {
        const cells = row.querySelectorAll(pair);

        [cells[0].textContent, cells[1].textContent] = [cells[1].textContent, cells[0].textContent];
        cells.forEach(cell => cell.classList.toggle("tax-flipped"));
    }

    function update() {
        document.documentElement.style.setProperty(
            "--page-background", getComputedStyle(document.body).backgroundColor);

        const table = document.querySelector("table.matches");

        if (table && table !== seen) {
            seen = table;
            [...table.tBodies[0].rows].forEach(row => CELL_PAIRS
                .filter(pair => flipped.has(flipKey(row, pair)))
                .forEach(pair => flipCells(row, pair)));
        }
    }

    document.addEventListener("click", event => {
        const root = document.documentElement.dataset;

        if (event.target.closest("table.matches .book-switch")) {
            if (root.evBook) delete root.evBook;
            else root.evBook = "superbet";

            return;
        }

        const toggle = event.target.closest("table.matches .tax-switch");

        if (!toggle) return;

        const margin = toggle.parentElement.matches(MARGIN_CELLS);
        const choice = margin ? "superbetMargin" : "superbetPrice";

        if (root[choice]) delete root[choice];
        else root[choice] = "net";
    });

    const switchedCell = target => target.closest("table.matches td[data-flip]");

    // The second press of a double click would select the word and flash blue.
    document.addEventListener("mousedown", event => {
        if (event.detail > 1 && switchedCell(event.target)) event.preventDefault();
    });

    document.addEventListener("dblclick", event => {
        const cell = switchedCell(event.target);

        if (!cell) return;

        const row = cell.parentElement;
        const pair = CELL_PAIRS.find(candidate => cell.matches(candidate));
        const key = flipKey(row, pair);

        if (!flipped.delete(key)) flipped.add(key);

        flipCells(row, pair);
    });

    new MutationObserver(update).observe(document.body, {childList: true, subtree: true});
    update();
})();
</script>"""


# ------------------------------------------------------------------- data ---


def list_sessions(sessions_dir: Path = SESSIONS_DIR) -> list[str]:
    """Session IDs, newest first. The IDs start with their UTC time.

    A run writes its status file before any row, so a new session shows up
    before its CSV exists.
    """
    stems = {path.stem.removesuffix("_status") for path in sessions_dir.glob("session_*")}

    return sorted((stem for stem in stems if SESSION_ID.match(stem)), reverse=True)


def pick_sessions(
    selected: str,
    sessions: list[str],
    has_rows: Callable[[str], bool],
    is_running: Callable[[str], bool],
) -> tuple[str | None, str | None]:
    """The sessions behind the header and behind the table.

    They differ only while a followed run has no row yet: its table would be
    empty for the whole Pinnacle and browser start, so the previous run's stays.
    """
    if selected != LATEST:
        return selected, selected

    if not sessions:
        return None, None

    newest = sessions[0]

    if has_rows(newest) or not is_running(newest):
        return newest, newest

    return newest, next((old for old in sessions[1:] if has_rows(old)), newest)


def session_label(session_id: str) -> str:
    """The session's start in local time."""
    match = SESSION_ID.match(session_id)

    if match is None:
        return session_id

    started = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")

    return (
        started.replace(tzinfo=timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def tour_of(tournament: Any) -> str:
    name = str(tournament or "").strip().lower()

    for prefix, tour in TOUR_PREFIXES.items():
        if name.startswith(prefix + " "):
            return tour

    return OTHER_TOUR


def draw_of(tournament: Any) -> str:
    stage = str(tournament or "").rpartition(" - ")[2].strip()

    return stage if stage in ("Qualifiers", "Doubles") else "Main"


def local_start(value: Any) -> datetime | None:
    """Pinnacle's scheduled start as naive local time, which sorts and displays
    without a timezone conversion in the browser."""
    start = parse_timestamp(value)

    return start.astimezone().replace(tzinfo=None) if start else None


def net_margin(odds1: Any, odds2: Any) -> float | None:
    """Margin of prices after tax; unlike overround it allows odds below 1.0."""
    first, second = number(odds1), number(odds2)

    return 1 / first + 1 / second - 1 if first and second else None


def table_record(row: dict) -> dict:
    """One display row. Every side-specific value is for the BET ON side; a match
    with no EV at either book has no side, so it lists player 1 with no prices."""
    side = table_side(row)
    bet_on = side or 1
    other = 2 if bet_on == 1 else 1

    def for_side(name: str) -> float | None:
        return number(row.get(name)) if side else None

    betclic_ev = for_side(f"ev{bet_on}")
    superbet_ev = for_side(f"superbet_ev{bet_on}")
    evs = [ev for ev in (betclic_ev, superbet_ev) if ev is not None]
    tournament = row.get("pinnacle_tournament", "")

    return {
        "Bet on": row.get(f"pinnacle_player{bet_on}", ""),
        "Opponent": row.get(f"pinnacle_player{other}", ""),
        "Best EV": max(evs, default=None),
        "Pinnacle": for_side(f"pinnacle_odds{bet_on}"),
        "Betclic": for_side(f"betclic_odds_for_pin_p{bet_on}"),
        "Superbet": for_side(f"superbet_price{bet_on}"),
        "Superbet net": for_side(f"superbet_odds{bet_on}"),
        "EV Betclic": betclic_ev,
        "EV Superbet": superbet_ev,
        "Margin Pinnacle": overround(
            row.get("pinnacle_odds1"),
            row.get("pinnacle_odds2"),
        ),
        "Margin Betclic": overround(
            row.get("betclic_odds_for_pin_p1"),
            row.get("betclic_odds_for_pin_p2"),
        ),
        "Margin Superbet": overround(
            row.get("superbet_price1"),
            row.get("superbet_price2"),
        ),
        "Margin Superbet net": net_margin(
            row.get("superbet_odds1"),
            row.get("superbet_odds2"),
        ),
        "Fair prob": for_side(f"probability{bet_on}"),
        **{
            column: for_side(f"{prefix}{ev_column(method, bet_on)}")
            for method, label in OTHER_EV_COLUMNS.items()
            for column, prefix in (
                (f"{label} Betclic", ""),
                (f"{label} Superbet", "superbet_"),
            )
        },
        "Starts": local_start(row.get("pinnacle_starts_at")),
        "Tour": tour_of(tournament),
        "Draw": draw_of(tournament),
        "Tournament": tournament,
        "Match conf": row.get("confidence", ""),
    }


def table_records(rows: list[dict]) -> list[dict]:
    """One record per match, best EV of either book first."""
    latest = sorted(latest_per_match(rows), key=sort_key)

    return [table_record(row) for row in latest]


def filter_records(
    records: list[dict],
    ev_filter: str = EV_ALL,
    min_ev: float = 0.0,
    tours: list[str] | None = None,
    draws: list[str] | None = None,
    player: str = "",
) -> list[dict]:
    """None means no restriction; an empty list selects nothing."""
    needle = player.strip().casefold()

    def keep(record: dict) -> bool:
        best = record["Best EV"]

        if ev_filter != EV_ALL and best is None:
            return False

        if ev_filter == EV_POSITIVE and best <= 0:
            return False

        if ev_filter == EV_THRESHOLD and best < min_ev:
            return False

        if tours is not None and record["Tour"] not in tours:
            return False

        if draws is not None and record["Draw"] not in draws:
            return False

        names = f"{record['Bet on']} {record['Opponent']}".casefold()

        return needle in names

    return [record for record in records if keep(record)]


def left_the_feed(info: dict) -> bool:
    """Whether the finished run used another collector or fell back from the feed.
    Both are only recorded when the run ends, so this is False during a run."""
    betclic = (info.get("performance") or {}).get("betclic") or {}

    if betclic.get("mode") not in (None, "feed"):
        return True

    return any(phase.get("name") != "feed" for phase in betclic.get("phases") or [])


def superbet_no_ev(latest: list[dict]) -> int:
    """FOUND on Superbet with no EV. None in the CSVs so far, but it would be a problem."""
    return sum(
        row.get("superbet_status") == "FOUND" and number(row.get("superbet_best_ev")) is None
        for row in latest
    )


def session_summary(rows: list[dict], info: dict, min_ev: float, now: datetime) -> dict:
    """Header values for a session. The odds age always runs up to now, even for
    a finished session: how old the prices are is what matters when betting."""
    status = str(info.get("status", "running")).lower()

    betclic_times = [
        stamp
        for stamp in (parse_timestamp(row.get("betclic_captured_at")) for row in rows)
        if stamp
    ]
    pinnacle_times = [
        stamp
        for stamp in (parse_timestamp(row.get("pinnacle_captured_at")) for row in rows)
        if stamp
    ]
    start = parse_timestamp(info.get("session_start"))
    expected = pinnacle_info(info).get("matches")

    last_activity = max(betclic_times, default=start)
    idle = (now - last_activity).total_seconds() if last_activity else None

    args = SimpleNamespace(expected=expected, min_ev=min_ev, max_age=STALE_MINUTES)
    latest = latest_per_match(rows)

    stats = []
    total = expected or len(latest)
    items = stats_items(rows, latest, args)
    superbet_found = next(
        (value for label, _, value in items if label == "Superbet found"), None
    )

    for label, _, value in items:
        if label in ("Unique matches", "Not found", "Superbet found"):
            continue

        if label == "Found on Betclic":
            stats.append((label, value))
            if superbet_found is not None:
                stats.append(("Found on Superbet", f"{superbet_found} / {total}"))
                stats.append((SUPERBET_NO_EV_LABEL, str(superbet_no_ev(latest))))
            continue

        # Above 0 it always shows: a match that was not properly checked.
        if label == "Errors" and value == "0" and not left_the_feed(info):
            continue

        if label == NO_EV_LABEL:
            stats.append(("FOUND but no EV", value))
            stats.extend(
                (f"{SUBGROUP_INDENT}{reason}", str(found))
                for reason, found in no_ev_reasons(latest).items()
            )
            continue

        if label == "EV calculated":
            value = f"{value} / {total}"

        stats.append((label, value))

    return {
        "status": status,
        "running": status not in FINISHED_STATES,
        "processed": len(rows),
        "expected": expected,
        "pinnacle_age": (
            (now - min(pinnacle_times)).total_seconds() if pinnacle_times else None
        ),
        "idle": idle,
        "stats": stats,
        "matches": len(latest),
        "total": total,
    }


# ----------------------------------------------------------------- display ---


def ev_css(value: Any, min_ev: float) -> str:
    """Same thresholds as live_monitor.ev_style."""
    parsed = number(value)

    if parsed is None or parsed != parsed:
        return ""
    if parsed < 0:
        return EV_RED
    if parsed == 0:
        return EV_GRAY

    return EV_AMBER if parsed < min_ev else EV_GREEN


def visible_columns(columns: list[str], superbet: bool) -> list[str]:
    """Superbet's columns only for sessions that have its data; no filter columns."""
    superbet_only = {
        "EV Superbet",
        *(column for column, name in EV_BOOK_CLASSES.items() if name == "ev-superbet"),
        *TAX_SWITCH_CLASSES,
    }

    return [
        column
        for column in columns
        if column not in FILTER_COLUMNS and (superbet or column not in superbet_only)
    ]


def percent(value: float) -> str:
    return f"{value:.2%}"


def short_doubles(name: str) -> str:
    """'Irene Burillo Escorihuela / Julia Riera' -> 'I. Escorihuela / J. Riera', since
    doubles names set the width of both name columns. Singles names stay whole."""
    if " / " not in name:
        return name

    tokens = [partner.split() for partner in name.split(" / ")]

    return " / ".join(
        f"{words[0][0]}. {words[-1]}" if len(words) > 1 else " ".join(words)
        for words in tokens
    )


CELL_FORMATS: dict[str, Callable[[Any], str]] = {
    "Bet on": short_doubles,
    "Opponent": short_doubles,
    "EV Betclic": format_ev,
    "EV Superbet": format_ev,
    **{column: format_ev for column in EV_BOOK_CLASSES},
    "Betclic": format_odds,
    "Superbet": format_odds,
    "Superbet net": format_odds,
    "Pinnacle": format_odds,
    "Margin Betclic": percent,
    "Margin Superbet": percent,
    "Margin Superbet net": percent,
    "Margin Pinnacle": percent,
    "Fair prob": format_probability,
    "Starts": lambda start: start.strftime("%a %H:%M"),
}


def cell_css(column: str, value: Any, dimmed: bool, min_ev: float) -> str:
    """A value's own colour wins over the row's."""
    if column.startswith("EV") and (css := ev_css(value, min_ev)):
        return css

    if dimmed:
        return DIMMED

    return BET_ON if column == "Bet on" else ""


def table_html(records: list[dict], min_ev: float, superbet: bool) -> str:
    """data-flip marks a cell a double click can flip to its other tax version."""
    columns = visible_columns(list(records[0]), superbet)
    numeric = {column for column in columns if column not in TEXT_COLUMNS | {"Starts"}}

    def attribute(name: str, value: Any) -> str:
        return f' {name}="{html.escape(str(value))}"' if value else ""

    def classes(column: str) -> str:
        names = ["number"] if column in numeric else []
        names += [TAX_SWITCH_CLASSES[column]] if column in TAX_SWITCH_CLASSES else []
        # Without Superbet's data no switch can bring Betclic's back once it is off.
        names += [EV_BOOK_CLASSES[column]] if superbet and column in EV_BOOK_CLASSES else []

        return attribute("class", " ".join(names))

    heads = "".join(
        f"<th{classes(column)}"
        f"{attribute('title', COLUMN_HELP.get(column))}>{column}"
        f"{TAX_SWITCH if column in TAX_SWITCH_CLASSES else ''}"
        f"{BOOK_SWITCH if superbet and column in BOOK_SWITCH_COLUMNS else ''}</th>"
        for column in columns
    )

    def cell(column: str, record: dict, dimmed: bool) -> str:
        value = record[column]
        blank = value is None or value == ""
        format_cell = CELL_FORMATS.get(column, str)
        text = "--" if value is None else format_cell(value)
        full_name = column in NAME_COLUMNS and text != value

        return (
            f"<td{classes(column)}"
            f"{attribute('style', cell_css(column, value, dimmed, min_ev))}"
            f"{attribute('title', value if full_name else None)}"
            f"{attribute('data-flip', column in TAX_SWITCH_CLASSES and not blank)}>"
            f"{html.escape(str(text))}</td>"
        )

    rows = "".join(
        "<tr>"
        + "".join(
            cell(column, record, record["EV Betclic"] is None and record["EV Superbet"] is None)
            for column in columns
        )
        + "</tr>"
        for record in records
    )

    return (
        f'<table class="matches"><thead><tr>{heads}</tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


def share(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else "--"


def overview_counts(value: str) -> tuple[int, int | None]:
    """'99 / 110' -> (99, 110) and '99' -> (99, None)."""
    part, _, whole = value.partition(" / ")

    return int(part), int(whole) if whole else None


def overview_meters(stats: dict, total: int) -> str:
    rows = [
        ("Processed", "Pinnacle matches processed"),
        ("Betclic", "Found on Betclic"),
        ("Superbet", "Found on Superbet"),
    ]
    cells = []

    for label, key in rows:
        if key not in stats:
            continue

        part, whole = overview_counts(stats[key])
        whole = whole or total
        width = min(100.0, 100 * part / whole) if whole else 0
        text = f"{part} / {whole}" if whole else str(part)

        cells.append(
            f"<div>{label}</div>"
            f'<div class="overview-meter" title="{key}: {text} ({share(part, whole)})">'
            f'<div style="width: {width:.1f}%; background-color: {OVERVIEW_BLUE}"></div></div>'
            f'<div class="count">{text}</div>'
        )

    return f'<div class="overview-meters">{"".join(cells)}</div>'


def overview_split(calculated: int, no_ev: int, reasons: str, matches: int, total: int) -> str:
    """Where every Pinnacle match ended up at one book. VERIFY, AMBIGUOUS and ERROR count
    as not matched, because EV is calculated for FOUND only."""
    parts = [
        ("EV calculated", calculated, OVERVIEW_BLUE, ""),
        ("No EV", no_ev, OVERVIEW_NO_EV, reasons),
        ("Not matched", max(matches - calculated - no_ev, 0), OVERVIEW_NOT_MATCHED, ""),
        ("Waiting", max(total - matches, 0), OVERVIEW_WAITING, ""),
    ]
    # A part at zero draws nothing. Waiting only means something during a run.
    drawn = [part for part in parts if part[1]]
    whole = sum(count for _, count, _, _ in drawn)
    blocks = "".join(
        f'<div style="flex: {count} 0 0; background-color: {color}" '
        f'title="{label}: {count} of {whole} ({share(count, whole)})"></div>'
        for label, count, color, _ in drawn
    )
    legend = "".join(
        f'<span><span class="overview-swatch" style="background-color: {color}"></span>'
        f"{label} <b>{count}</b>"
        + (f' <span class="overview-note">({html.escape(note)})</span>' if note else "")
        + "</span>"
        for label, count, color, note in parts
        if count or label != "Waiting"
    )
    names = ", ".join(f"{label} {count}" for label, count, _, _ in drawn)

    return (
        f'<div class="overview-split" role="img" aria-label="{html.escape(names)}">'
        f'{blocks}</div><div class="overview-legend">{legend}</div>'
    )


def overview_tile(label: str, value: str, help_text: str, css: str = "") -> str:
    style = f' style="{css}"' if css else ""

    return (
        f'<div class="overview-tile" title="{html.escape(help_text)}">'
        f'<div class="value"{style}>{html.escape(value)}</div>'
        f'<div class="label">{html.escape(label)}</div></div>'
    )


def overview_positive(stats: dict) -> str:
    """Colours follow the table: amber above 0, green from the threshold up."""

    def hit(value: str, css: str) -> str:
        return css.split(";")[0] if int(value) else ""

    threshold = next(label for label in stats if label.startswith("EV(ref) >= "))
    limit = threshold.removeprefix("EV(ref) >= ")
    tiles = [
        overview_tile(
            "Betclic > 0%",
            stats["EV(ref) > 0%"],
            "Betclic matches with EV above 0",
            hit(stats["EV(ref) > 0%"], EV_AMBER),
        ),
        overview_tile(
            f"Betclic \u2265 {limit}",
            stats[threshold],
            "Betclic matches with EV at or above the threshold set in the sidebar",
            hit(stats[threshold], EV_GREEN),
        ),
    ]

    if "Superbet EV(ref) > 0%" in stats:
        superbet_threshold = stats[f"Superbet {threshold}"]
        tiles += [
            overview_tile(
                "Superbet > 0%",
                stats["Superbet EV(ref) > 0%"],
                "Superbet matches with EV above 0, after tax",
                hit(stats["Superbet EV(ref) > 0%"], EV_AMBER),
            ),
            overview_tile(
                f"Superbet ≥ {limit}",
                superbet_threshold,
                "Superbet matches with EV at or above the threshold set in the sidebar, after tax",
                hit(superbet_threshold, EV_GREEN),
            ),
        ]

    return f'<div class="overview-tiles">{"".join(tiles)}</div>'


def overview_checks(stats: dict) -> str:
    """A warning sign as well as the colour, so a problem does not rest on colour alone."""
    stale = next(label for label in stats if label.startswith("Stale"))
    checks = [("Stale", stats[stale], stale)]

    # Only in the stats when it matters: see session_summary.
    if "Errors" in stats:
        checks.append(("Errors", stats["Errors"], "Matches that could not be checked"))

    def tile(label: str, value: str, help_text: str) -> str:
        bad = int(value) > 0

        return overview_tile(
            label, f"\u26a0 {value}" if bad else value, help_text, EV_AMBER if bad else ""
        )

    return f'<div class="overview-tiles">{"".join(tile(*check) for check in checks)}</div>'


def overview_html(summary: dict) -> str:
    """The stats of session_summary as two rows of cards. Bars, not pies: the counts are
    often close together (99 and 105 of 110), which pie slices do not show."""
    stats = dict(summary["stats"])
    matches, total = summary["matches"], summary["total"]
    betclic_reasons = ", ".join(
        f"{label.strip()} {value}"
        for label, value in stats.items()
        if label.startswith(SUBGROUP_INDENT)
    )

    def card(heading: str, body: str, size: str = "") -> str:
        return (
            f'<div class="overview-card {size}">'
            f'<div class="overview-heading">{html.escape(heading)}</div>{body}</div>'
        )

    coverage = [
        card("Pinnacle matches covered", overview_meters(stats, total), "wide"),
        card(
            "Betclic EV status",
            overview_split(
                overview_counts(stats["EV calculated"])[0],
                int(stats["FOUND but no EV"]),
                betclic_reasons,
                matches,
                total,
            ),
            "wide",
        ),
    ]

    if "Found on Superbet" in stats:
        coverage.append(
            card(
                "Superbet EV status",
                overview_split(
                    overview_counts(stats["Found on Superbet"])[0],
                    int(stats[SUPERBET_NO_EV_LABEL]),
                    "",
                    matches,
                    total,
                ),
                "wide",
            )
        )

    results = [
        card(f"Positive EV ({REFERENCE_METHOD})", overview_positive(stats), "wide"),
        card("Checks", overview_checks(stats), "wide"),
    ]
    if len(coverage) > len(results):
        results.append('<div class="overview-card wide spacer"></div>')

    return (
        '<section class="overview"><div class="overview-title">Overview</div>'
        f'<div class="overview-cards">{"".join(coverage)}</div>'
        f'<div class="overview-cards">{"".join(results)}</div></section>'
    )


COLUMN_HELP = {
    "Bet on": "Side with the best EV at either book. Every value in the row is for "
    "this side. A match with no EV lists player 1 and no prices.",
    "EV Betclic": "Power devig, the reference method.",
    "EV Superbet": "Power devig on Superbet's price after its 12% tax.",
    "Superbet": "Price as Superbet shows it, before tax. "
    "Double-click a price to flip that cell to the other one.",
    "Superbet net": "Price after Superbet's 12% tax. This drives its EV. "
    "Double-click a price to flip that cell to the other one.",
    "Margin Superbet": "On prices as Superbet shows them, before tax. "
    "Double-click a margin to flip that cell to the other one.",
    "Margin Superbet net": "On prices after Superbet's 12% tax. "
    "Double-click a margin to flip that cell to the other one.",
    "Fair prob": "Pinnacle's power-devigged probability of this side.",
    "Starts": "Pinnacle's scheduled start in local time. It can move.",
    "Match conf": "How well the Betclic match fit the Pinnacle names.",
    **{
        f"{label} Betclic": f"Betclic EV with {method.replace('_', ' ')} devig."
        for method, label in OTHER_EV_COLUMNS.items()
    },
    **{
        f"{label} Superbet": f"Superbet EV with {method.replace('_', ' ')} devig, "
        "on its price after its 12% tax."
        for method, label in OTHER_EV_COLUMNS.items()
    },
}


def session_csv(session_id: str) -> Path:
    return SESSIONS_DIR / f"{session_id}.csv"


def session_info(session_id: str) -> dict:
    return read_runner_info(SESSIONS_DIR / f"{session_id}_status.json")


def csv_has_rows(session_id: str) -> bool:
    """The worker writes a new file's header together with its first row."""
    try:
        return session_csv(session_id).stat().st_size > 0
    except OSError:
        return False


def is_running(session_id: str) -> bool:
    status = str(session_info(session_id).get("status", "running")).lower()

    return status not in FINISHED_STATES


def current_sessions(selected: str) -> tuple[str | None, str | None]:
    return pick_sessions(selected, list_sessions(), csv_has_rows, is_running)


def load_rows(st: Any, session_id: str) -> tuple[list[dict], Any]:
    """The session's rows and the file state they came from, read only when the
    file changed. A failed read means the worker is writing: keep the last good rows."""
    path = session_csv(session_id)
    cache = st.session_state.setdefault("rows_cache", {})

    try:
        stat = path.stat()
        signature = (session_id, stat.st_mtime_ns, stat.st_size)
    except OSError:
        signature = (session_id, None, None)

    if cache.get("signature") != signature:
        fresh = read_session_rows(path)

        if fresh is not None:
            cache.update(signature=signature, rows=fresh)
        elif cache.get("signature", (None,))[0] != session_id:
            cache.update(signature=None, rows=[])

    return cache.get("rows", []), cache.get("signature")


def render_header(
    st: Any, session_id: str, table_session: str, rows: list[dict], min_ev: float
) -> None:
    """Runs every second, so it stays cheap. rows belong to table_session."""
    info = session_info(session_id)
    now = datetime.now(timezone.utc)
    waiting = table_session != session_id
    summary = session_summary([] if waiting else rows, info, min_ev, now)

    parts = [f"**{summary['status'].upper()}**", session_label(session_id)]

    if summary["pinnacle_age"] is not None:
        parts.append(f"Pinnacle odds {fmt_age(summary['pinnacle_age'])} old")

    st.markdown(" · ".join(parts))

    if info.get("error"):
        st.error(info["error"])

    if summary["running"]:
        if summary["expected"]:
            st.progress(
                min(1.0, summary["processed"] / summary["expected"]),
                text=f"{summary['processed']} / {summary['expected']} matches",
            )

        if summary["idle"] is not None and summary["idle"] > STUCK_AFTER_SECONDS:
            st.warning(
                f"No new row for {fmt_age(summary['idle'])}. "
                "The run may have stopped without updating its status."
            )

    st.html(overview_html(summary))

    if waiting:
        age = session_summary(rows, {}, min_ev, now)["pinnacle_age"]
        prices = f", its Pinnacle odds {fmt_age(age)} old" if age is not None else ""

        st.warning(
            "Waiting for this run's first match. The table still shows the previous "
            f"run ({session_label(table_session)}{prices})."
        )


def render_table(box: Any, session_id: str, rows: list[dict], filters: dict) -> None:
    min_ev = filters["min_ev"]
    records = table_records(rows)
    shown = filter_records(
        records,
        filters["ev_filter"],
        min_ev,
        filters["tours"],
        filters["draws"],
        filters["player"],
    )

    box.caption(f"Showing {len(shown)} of {len(records)} matches")

    if not shown:
        waiting = is_running(session_id) and not records
        box.info("Waiting for the first match..." if waiting else "No match fits the filters.")
        return

    box.html(table_html(shown, min_ev, has_superbet(rows)))


def wait_for_browsers_to_close(
    open_tabs: Callable[[], int], sleep: Callable[[float], None] = time.sleep
) -> None:
    """Returns once every tab has been gone for STOP_AFTER_SECONDS. It counts its own
    one-second steps, not the clock, so time the computer spends asleep does not count."""
    seen = False
    empty_for = 0

    while not seen or empty_for < STOP_AFTER_SECONDS:
        sleep(1)

        if open_tabs():
            seen = True
            empty_for = 0
        else:
            empty_for += 1


def stop_server_when_browsers_close() -> None:
    from streamlit import runtime

    # Streamlit has no public count. If this attribute ever goes away, only this
    # thread dies: the server keeps running and Ctrl+C still stops it.
    wait_for_browsers_to_close(runtime.get_instance()._session_mgr.num_active_sessions)

    print("  Browser closed, stopping the dashboard.", flush=True)
    # The dashboard writes nothing, so there is nothing to shut down cleanly.
    os._exit(0)


def start_browser_watch() -> None:
    threading.Thread(target=stop_server_when_browsers_close, daemon=True).start()


def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="Tennis EV", layout="wide")
    st.html(
        NO_STALE_FADE + TABLE_STYLE + OVERVIEW_STYLE + TABLE_SCRIPT,
        unsafe_allow_javascript=True,
    )

    # Cached so that the whole server, across tabs and reruns, starts one watcher.
    st.cache_resource(show_spinner=False)(start_browser_watch)()

    with st.sidebar:
        selected = st.selectbox(
            "Session",
            [LATEST, *list_sessions()],
            format_func=lambda value: value if value == LATEST else session_label(value),
        )
        ev_filter = st.radio("Show", EV_FILTERS, horizontal=True)
        threshold = st.number_input(
            "EV threshold (%)",
            min_value=0.0,
            value=DEFAULT_MIN_EV_PERCENT,
            step=0.5,
            help="Green from here up. Also the limit of the 'EV >= threshold' filter.",
        )
        tours = st.pills("Tour", TOURS, selection_mode="multi", default=TOURS)
        draws = st.pills("Draw", DRAWS, selection_mode="multi", default=DRAWS)
        player = st.text_input("Player")

    filters = {
        "ev_filter": ev_filter,
        "min_ev": threshold / 100,
        "tours": tours,
        "draws": draws,
        "player": player,
    }

    header_box = st.container()
    table_box = st.container()

    first_header, table_session = current_sessions(selected)
    running = bool(first_header) and is_running(first_header)
    rows, drawn = load_rows(st, table_session) if table_session else ([], None)

    @st.fragment(run_every=REFRESH_SECONDS if running else IDLE_REFRESH_SECONDS)
    def live_header() -> None:
        """A fragment leaves the rest of the page alone, so only a full run redraws
        the table, and this asks for one when the file has changed. So does a run
        starting or ending, because only a full run can change this fragment's pace."""
        header_session, newest_table = current_sessions(selected)
        fresh, signature = load_rows(st, newest_table) if newest_table else ([], None)

        still_running = bool(header_session) and is_running(header_session)

        if signature != drawn or still_running != running:
            st.rerun(scope="app")

        if header_session:
            render_header(st, header_session, newest_table, fresh, filters["min_ev"])

    # Header first: it is quick, and the table can take a second or two.
    with header_box:
        live_header()

    if table_session:
        render_table(table_box, table_session, rows, filters)
    else:
        table_box.info(f"No sessions yet in {SESSIONS_DIR}. Start scripts/run_once.py.")


def launch() -> int:
    """Start the Streamlit server on this file. It runs from the repo root because
    .streamlit/config.toml, which binds it to localhost, is read from there."""
    command = [sys.executable, "-m", "streamlit", "run", str(Path(__file__).resolve())]

    try:
        return subprocess.call(command, cwd=ROOT)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    from streamlit import runtime

    if runtime.exists():
        main()
    else:
        sys.exit(launch())
