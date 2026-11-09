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
With DASHBOARD_KEEP_RUNNING=1 in the environment it never stops by itself, for a
server that runs as a service (it is then stopped like any other process).

Each table row is one match (latest observation), for the BET ON side, as in
the terminal table. It is a plain HTML table, not st.dataframe: that one draws on a
canvas as tall as the table and repaints it while scrolling. The page's own
scrollbars move this one, so the browser scrolls it without repainting anything.
"""

from __future__ import annotations

import calendar
import html
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timezone
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
    fmt_gap,
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
# Set to 1 to turn the browser watcher off, for a dashboard that runs as a service.
KEEP_RUNNING_ENV = "DASHBOARD_KEEP_RUNNING"

# live_monitor's defaults for --min-ev and --max-age.
DEFAULT_MIN_EV_PERCENT = 2.0
STALE_MINUTES = 15.0

# Markdown strips leading spaces from a line, but not this character.
SUBGROUP_INDENT = " "

LATEST = "Latest (follows new runs)"

# Session State keys of the session picker: the chosen session (LATEST or an ID),
# the popover's open state, the month on show and the day whose sessions are listed.
SESSION_KEY = "session"
PICKER_KEY = "session_picker"
MONTH_KEY = "picker_month"
DAY_KEY = "picker_day"
CHOSEN_KEY = "picker_chosen"
# Counts resets, so each one sends the browser a new script (see reset_settings).
RESET_KEY = "settings_reset"
SESSIONS_PER_ROW = 3
WEEKDAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]

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
TOURS = ["ATP", "WTA", "Challenger", "WTA 125", "ITF Men", "ITF Women"]
DRAWS = ["Main", "Qualifiers", "Doubles"]
# First pill of Tour and Draw: no restriction, so it is the only way to see tours
# outside TOURS (OTHER_TOUR). Picking it clears the others; picking another clears
# it, and clearing the last one picks it again (user, 5 Nov 2026).
ALL_PILL = "All"

# The bookmakers the page shows (user, 5 Nov 2026). A book turned off loses its
# columns and overview cards, and its prices no longer pick Bet on or Best EV.
BOOKS = ["Betclic", "Superbet"]
# Betclic's fields in a session row; Superbet's all start with "superbet_".
BETCLIC_FIELDS = re.compile(
    r"^(ev\d(_.+)?|best_ev|best_player|betclic_odds_for_pin_p\d|betclic_margin)$"
)

MAX_MIN_EV_PERCENT = 2.0
SCOPE_SESSION = "This session"
SCOPE_ALL = "All sessions"

# Session State keys of the sidebar settings and their defaults. Picking Latest
# puts them all back (user, 5 Nov 2026), along with the table's ⇄ and double-click
# switches in the browser.
SETTING_DEFAULTS = {
    "with_ev": False,
    "ev_scope": SCOPE_SESSION,
    "min_ev": DEFAULT_MIN_EV_PERCENT,
    "show_result": False,
    "show_clv": False,
    "books": list(BOOKS),
    "tours": [ALL_PILL],
    "draws": [ALL_PILL],
    "player": "",
    "player_all": False,
}

NAME_COLUMNS = {"Bet on", "Opponent"}

TEXT_COLUMNS = {
    *NAME_COLUMNS,
    "Tournament",
    "Match conf",
    "Session",
    "Result",
}

# Files behind the CLV and Result columns: Pinnacle's snapshots (every match, so
# also prices after the last session) and the finished matches.
PINNACLE_RAW_DIR = ROOT / "data" / "pinnacle" / "raw"
MATCH_RESULTS_FILES = [
    ROOT / "data" / "match_results" / "results.csv",
    ROOT / "data" / "match_results" / "state.json",
]

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
/* Less empty space above the first sidebar widget (the collapse button's row). */
[data-testid="stSidebarHeader"] {
    padding-top: 0.5rem !important;
    padding-bottom: 0 !important;
    margin-bottom: 0 !important;
    height: auto !important;
}
/* Streamlit also leaves 6rem below the last one (the About button); 2.5rem is
   about the gap above it, under the Draw pills. */
[data-testid="stSidebarUserContent"] {
    padding-top: 0 !important;
    padding-bottom: 2.5rem !important;
}
/* Same above the session bar: Streamlit leaves 6rem for its header, which here
   holds only the menu at the right, so it goes see-through over the page. */
[data-testid="stMainBlockContainer"] {
    padding-top: 1.5rem !important;
}
[data-testid="stHeader"] {
    background: transparent !important;
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
[data-testid="stMain"]::-webkit-scrollbar-track:horizontal {
    margin-right: var(--scrollbar-cut, 0px);
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
STATUS_COLOURS = {"finished": "#1f9d55", "failed": "#d64545", "interrupted": OVERVIEW_NO_EV}
OVERVIEW_STYLE = """<style>
.session-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px 20px;
    font-size: 14px;
    line-height: 24px;
}
.session-status {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 0 11px;
    border-radius: 12px;
    font-weight: 600;
    color: var(--status);
    background-color: color-mix(in srgb, var(--status) 15%, transparent);
}
.session-status::before {
    content: "";
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background-color: currentColor;
}
.session-fact { font-variant-numeric: tabular-nums; font-weight: 600; }
.session-fact .label {
    margin-right: 6px;
    font-weight: 400;
    color: color-mix(in srgb, currentColor 65%, transparent);
}
.session-age {
    margin-left: -8px;
    font-variant-numeric: tabular-nums;
    font-weight: 600;
}
.fresh { color: #1f9d55; }
.stale { color: #d64545; }
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
.overview-card.fit { flex: 0 1 auto; }
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

    // The horizontal scrollbar's track ends a third of the page width early (user,
    // 5 Nov 2026). A track margin takes no percentage, so this keeps it in pixels.
    let sized = null;
    const sizer = new ResizeObserver(([entry]) => entry.target.style.setProperty(
        "--scrollbar-cut", `${Math.round(entry.target.clientWidth / 3)}px`));

    function update() {
        document.documentElement.style.setProperty(
            "--page-background", getComputedStyle(document.body).backgroundColor);

        const main = document.querySelector('[data-testid="stMain"]');

        if (main && main !== sized) {
            if (sized) sizer.unobserve(sized);
            sized = main;
            sizer.observe(main);
        }

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

    // Picking Latest calls this: every switch back to its default, as after a reload.
    window.resetMatchesTable = () => {
        const root = document.documentElement.dataset;
        const table = document.querySelector("table.matches");

        delete root.evBook;
        delete root.superbetPrice;
        delete root.superbetMargin;

        if (table) {
            [...table.tBodies[0].rows].forEach(row => CELL_PAIRS
                .filter(pair => flipped.has(flipKey(row, pair)))
                .forEach(pair => flipCells(row, pair)));
        }

        flipped.clear();
    };

    new MutationObserver(update).observe(document.body, {childList: true, subtree: true});
    update();

    // The same format as pinnacle_age_text.
    function ageText(seconds, staleAfter) {
        const pad = value => String(value).padStart(2, "0");
        const minutes = Math.floor(seconds / 60);

        if (seconds < 60) return `${seconds} s`;
        if (seconds >= 3600) return `${Math.floor(minutes / 60)} h ${pad(minutes % 60)} min`;

        return seconds > staleAfter ? `${minutes} min` : `${minutes} min ${pad(seconds % 60)} s`;
    }

    // Counts in the browser, because a server tick each second makes scrolling stutter
    // (see IDLE_REFRESH_SECONDS). Writing the text node does not wake the observer above.
    setInterval(() => {
        const age = document.querySelector(".session-age [data-since]");

        if (!age || !age.firstChild) return;

        const seconds = Math.max(0, Math.round(Date.now() / 1000 - Number(age.dataset.since)));
        const staleAfter = Number(age.dataset.staleAfter);
        const shown = ageText(seconds, staleAfter);
        const stale = seconds > staleAfter;

        if (age.firstChild.nodeValue !== shown) age.firstChild.nodeValue = shown;

        // Green while fresh, red once stale: the age and the Performance card's flag.
        const flag = document.querySelector(".overview-tile [data-stale-flag]");
        [age.parentElement, flag].forEach(element => {
            if (!element || element.classList.contains("stale") === stale) return;
            element.classList.toggle("stale", stale);
            element.classList.toggle("fresh", !stale);
        });
        if (flag && flag.firstChild && flag.firstChild.nodeValue !== (stale ? "Yes" : "No")) {
            flag.firstChild.nodeValue = stale ? "Yes" : "No";
        }
    }, 250);
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


def session_started(session_id: str) -> datetime | None:
    """The session's start in local time; its ID holds it in UTC."""
    match = SESSION_ID.match(session_id)

    if match is None:
        return None

    started = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")

    return started.replace(tzinfo=timezone.utc).astimezone()


def session_label(session_id: str) -> str:
    """The session's start in local time."""
    started = session_started(session_id)

    return session_id if started is None else started.strftime("%Y-%m-%d %H:%M:%S")


def sessions_by_day(sessions: list[str]) -> dict[date, list[str]]:
    """Sessions per local day, earliest first within a day."""
    days: dict[date, list[str]] = {}

    for session_id in sorted(sessions):
        started = session_started(session_id)

        if started is not None:
            days.setdefault(started.date(), []).append(session_id)

    return days


def month_weeks(first: date) -> list[list[date | None]]:
    """The month's weeks from Monday, with None for days of the months around it."""
    weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month)

    return [[day if day.month == first.month else None for day in week] for week in weeks]


def shift_month(first: date, step: int) -> date:
    index = first.year * 12 + first.month - 1 + step

    return date(index // 12, index % 12 + 1, 1)


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
    superbet_best = superbet_ev is not None and (betclic_ev is None or superbet_ev > betclic_ev)
    # The price behind Best EV; Superbet's after tax, as its EV is.
    price = (
        for_side(f"superbet_odds{bet_on}")
        if superbet_best
        else for_side(f"betclic_odds_for_pin_p{bet_on}")
    )

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
        # Hidden (leading underscore): for the CLV and Result columns.
        "_price": price if evs else None,
        "_starts_at": row.get("pinnacle_starts_at") or "",
        "_captured_at": row.get("pinnacle_captured_at") or row.get("betclic_captured_at") or "",
    }


def only_books(row: dict, books: list[str]) -> dict:
    """The row with the fields of every book not in books blanked, as if that book
    had not been read."""
    betclic_off = "Betclic" not in books
    superbet_off = "Superbet" not in books

    if not betclic_off and not superbet_off:
        return row

    def off(field: str) -> bool:
        return (superbet_off and field.startswith("superbet_")) or (
            betclic_off and BETCLIC_FIELDS.match(field) is not None
        )

    return {field: "" if off(field) else value for field, value in row.items()}


def table_records(rows: list[dict], books: list[str] = BOOKS) -> list[dict]:
    """One record per match, best EV of the books shown first."""
    rows = [only_books(row, books) for row in rows]
    latest = sorted(latest_per_match(rows), key=sort_key)

    return [table_record(row) for row in latest]


def match_key(record: dict) -> tuple:
    """The same match in any session: the BET ON side can change between runs."""
    return frozenset((record["Bet on"], record["Opponent"])), record["Tournament"]


def with_session(record: dict, label: str) -> dict:
    """The record with a Session column after Opponent."""
    out = {}

    for column, value in record.items():
        out[column] = value

        if column == "Opponent":
            out["Session"] = label

    return out


def cached_session_records(
    session_id: str, cache: dict, books: list[str] = BOOKS
) -> list[dict]:
    """A session's records, read again only when its file changed. A failed read
    means the worker is writing: keep the last good records."""
    path = session_csv(session_id)
    key = (session_id, tuple(books))

    try:
        stat = path.stat()
    except OSError:
        return []

    signature = (stat.st_mtime_ns, stat.st_size)
    held = cache.get(key)

    if held and held[0] == signature:
        return held[1]

    rows = read_session_rows(path)

    if rows is None:
        return held[1] if held else []

    label = session_label(session_id)
    records = [with_session(record, label) for record in table_records(rows, books)]
    cache[key] = (signature, records)

    return records


def all_session_records(
    sessions: list[str], cache: dict, books: list[str] = BOOKS
) -> list[dict]:
    """Every session's records, in the order of sessions (newest first)."""
    return [
        record
        for session_id in sessions
        for record in cached_session_records(session_id, cache, books)
    ]


def latest_ev_matches(records: list[dict], min_ev: float) -> list[dict]:
    """Each match once, from the first (newest) session where its best EV reached
    min_ev, best EV first."""
    seen = set()
    matches = []

    for record in records:
        best = record["Best EV"]

        if best is None or best < min_ev or match_key(record) in seen:
            continue

        seen.add(match_key(record))
        matches.append(record)

    return sorted(matches, key=lambda record: -record["Best EV"])


def table_candidates(
    records: list[dict], all_records: list[dict] | None, filters: dict
) -> list[dict]:
    """The records the filters then narrow down. A player search over all sessions
    keeps every appearance (newest session first); the EV filter over all sessions
    keeps each match once, from its newest qualifying run."""
    if all_records is None:
        return records

    if filters.get("player_all"):
        return all_records

    return latest_ev_matches(all_records, filters["min_ev"])


def positive_ev_stats(records: list[dict], min_ev: float) -> dict:
    """The Positive EV card's counts over many sessions, each match counted once."""

    def count(column: str, low: float, strict: bool) -> str:
        keys = {
            match_key(record)
            for record in records
            if record[column] is not None
            and (record[column] > low if strict else record[column] >= low)
        }

        return str(len(keys))

    limit = f"EV(ref) >= {min_ev:.2%}"
    stats = {
        "EV(ref) > 0%": count("EV Betclic", 0.0, True),
        limit: count("EV Betclic", min_ev, False),
    }

    if any(record["EV Superbet"] is not None for record in records):
        stats["Superbet EV(ref) > 0%"] = count("EV Superbet", 0.0, True)
        stats[f"Superbet {limit}"] = count("EV Superbet", min_ev, False)

    return stats


def outcome_signature() -> tuple:
    """Changes when a Pinnacle snapshot is added or the results are updated."""
    try:
        snapshots = [path.stat().st_mtime_ns for path in PINNACLE_RAW_DIR.glob("*.csv")]
    except OSError:
        snapshots = []

    files = []
    for path in MATCH_RESULTS_FILES:
        try:
            stat = path.stat()
            files.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            files.append(None)

    return len(snapshots), max(snapshots, default=0), tuple(files)


def load_outcome_sources(cache: dict) -> dict:
    """Pinnacle's snapshots by match and the finished matches, read again only when
    outcome_signature changes (about 2 s for 250 snapshots and 11k results)."""
    signature = outcome_signature()

    if cache.get("signature") != signature:
        import evaluate_ev
        import match_results

        results = match_results.load_results() if match_results.RESULTS_CSV.exists() else []
        cache.update(
            signature=signature,
            sightings=evaluate_ev.load_sightings(),
            by_day=match_results.index_results(results),
            covered=match_results.read_covered_through(),
        )

    return cache


def known_start(record: dict, sightings: list[dict]) -> datetime | None:
    """The start can move: the last one Pinnacle listed, else this row's."""
    import evaluate_ev

    return evaluate_ev.latest_start([], sightings) or evaluate_ev.parse_time(record["_starts_at"])


def closing_line(
    record: dict, sightings: list[dict], start: datetime | None, now: datetime
) -> tuple[float | None, str]:
    """(CLV, tooltip). CLV = the price behind Best EV x Pinnacle's power-devigged
    probability at its last price before the start - 1, only once the match started."""
    import evaluate_ev
    import match_results

    from betting.margin import remove_margin

    if start is None or start > now or not record["_price"]:
        return None, ""

    tokens = match_results.name_tokens(record["Bet on"])

    for sighting in reversed(sightings):
        seen = evaluate_ev.parse_time(sighting["captured_at"])
        odds = [number(sighting.get("odds1")), number(sighting.get("odds2"))]
        side = evaluate_ev.side_of(sighting["player1"], sighting["player2"], tokens)

        if seen is None or seen >= start or not side or not all(o and o > 1 for o in odds):
            continue

        fair = remove_margin(odds, REFERENCE_METHOD)
        hours = (start - seen).total_seconds() / 3600

        return (
            record["_price"] * fair[side - 1] - 1,
            f"Closing Pinnacle {odds[side - 1]:.2f}, seen {hours:.1f} h before the start",
        )

    return None, ""


def match_outcome(
    record: dict,
    by_day: dict,
    covered: date | None,
    start: datetime | None,
    now: datetime,
) -> str:
    """Won or Lost for the BET ON side, like evaluate_ev.settle: Pending until the
    match can have been played and the results cover its day."""
    import match_results

    if start and start > now:
        return "Pending"

    reference = start.isoformat() if start else None
    link = match_results.link_match(
        record["Bet on"],
        record["Opponent"],
        by_day,
        starts_at=reference,
        captured_at=record["_captured_at"] or None,
        tournament=record["Tournament"],
    )

    if not link["linked"]:
        day = str(reference or record["_captured_at"])[:10]
        return "Pending" if covered is None or day >= covered.isoformat() else "No result"

    text = "Won" if link["winner_side"] == 1 else "Lost"
    result_type = link["result"].get("result_type") or "completed"

    return text if result_type == "completed" else f"{text} ({result_type})"


def add_outcomes(
    records: list[dict],
    sources: dict,
    now: datetime,
    clv: bool = True,
    result: bool = True,
) -> list[dict]:
    """The records with Result and CLV columns after the names (and Session)."""
    import evaluate_ev

    out = []

    for record in records:
        key = evaluate_ev.match_key(record["Bet on"], record["Opponent"], record["Tournament"])
        sightings = sources["sightings"].get(key, [])
        start = known_start(record, sightings)
        extra = {}

        if result:
            extra["Result"] = match_outcome(
                record, sources["by_day"], sources["covered"], start, now
            )

        if clv:
            extra["CLV"], extra["_title CLV"] = closing_line(record, sightings, start, now)

        anchor = "Session" if "Session" in record else "Opponent"
        new = {}

        for column, value in record.items():
            new[column] = value

            if column == anchor:
                new.update(extra)

        out.append(new)

    return out


def outcomes_cache() -> dict:
    """Held by st.cache_resource, so every tab and rerun shares one."""
    return {}


def records_cache() -> dict:
    """Held by st.cache_resource, so every tab and rerun shares one."""
    return {}


def filter_records(
    records: list[dict],
    with_ev: bool = False,
    min_ev: float = 0.0,
    tours: list[str] | None = None,
    draws: list[str] | None = None,
    player: str = "",
) -> list[dict]:
    """with_ev keeps only matches whose best EV is at least min_ev. None means no
    restriction; an empty list selects nothing."""
    needle = player.strip().casefold()

    def keep(record: dict) -> bool:
        best = record["Best EV"]

        if with_ev and (best is None or best < min_ev):
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


def capture_gap(latest: list[dict], column: str) -> float | None:
    """Median seconds from a match's Pinnacle capture to this book's capture."""
    gaps = []

    for row in latest:
        pinnacle = parse_timestamp(row.get("pinnacle_captured_at"))
        book = parse_timestamp(row.get(column))

        if pinnacle and book:
            gaps.append((book - pinnacle).total_seconds())

    return statistics.median(gaps) if gaps else None


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
        "pinnacle_captured": min(pinnacle_times).timestamp() if pinnacle_times else None,
        "idle": idle,
        "stats": stats,
        "matches": len(latest),
        "total": total,
        "betclic_gap": capture_gap(latest, "betclic_captured_at"),
        "superbet_gap": capture_gap(latest, "superbet_captured_at"),
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


BETCLIC_ONLY = {
    "Betclic",
    "EV Betclic",
    "Margin Betclic",
    "Match conf",
    *(column for column, name in EV_BOOK_CLASSES.items() if name == "ev-betclic"),
}
SUPERBET_ONLY = {
    "EV Superbet",
    *(column for column, name in EV_BOOK_CLASSES.items() if name == "ev-superbet"),
    *TAX_SWITCH_CLASSES,
}


def visible_columns(columns: list[str], superbet: bool, betclic: bool = True) -> list[str]:
    """Superbet's columns only for sessions that have its data, each book's only
    while it is turned on; no filter or hidden (leading underscore) columns."""
    return [
        column
        for column in columns
        if column not in FILTER_COLUMNS
        and not column.startswith("_")
        and (superbet or column not in SUPERBET_ONLY)
        and (betclic or column not in BETCLIC_ONLY)
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
    "CLV": format_ev,
}


def result_css(value: Any) -> str:
    text = str(value or "")

    if text.startswith("Won"):
        return EV_GREEN
    if text.startswith("Lost"):
        return EV_RED

    return DIMMED


def cell_css(column: str, value: Any, dimmed: bool, min_ev: float) -> str:
    """A value's own colour wins over the row's."""
    if column.startswith("EV") and (css := ev_css(value, min_ev)):
        return css

    # CLV is good above 0 whatever the EV threshold.
    if column == "CLV" and (css := ev_css(value, 0.0)):
        return css

    if column == "Result" and value:
        return result_css(value)

    if dimmed:
        return DIMMED

    return BET_ON if column == "Bet on" else ""


def table_html(
    records: list[dict], min_ev: float, superbet: bool, betclic: bool = True
) -> str:
    """data-flip marks a cell a double click can flip to its other tax version."""
    columns = visible_columns(list(records[0]), superbet, betclic)
    # The book switch only when both books' EV columns are there to switch between.
    both = superbet and betclic
    numeric = {column for column in columns if column not in TEXT_COLUMNS | {"Starts"}}

    def attribute(name: str, value: Any) -> str:
        return f' {name}="{html.escape(str(value))}"' if value else ""

    def classes(column: str) -> str:
        names = ["number"] if column in numeric else []
        names += [TAX_SWITCH_CLASSES[column]] if column in TAX_SWITCH_CLASSES else []
        # With one book no switch could bring the other's back once it is off.
        names += [EV_BOOK_CLASSES[column]] if both and column in EV_BOOK_CLASSES else []

        return attribute("class", " ".join(names))

    heads = "".join(
        f"<th{classes(column)}"
        f"{attribute('title', COLUMN_HELP.get(column))}>{column}"
        f"{TAX_SWITCH if column in TAX_SWITCH_CLASSES else ''}"
        f"{BOOK_SWITCH if both and column in BOOK_SWITCH_COLUMNS else ''}</th>"
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
            f"{attribute('title', value if full_name else record.get(f'_title {column}'))}"
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


def overview_meters(stats: dict, total: int, books: list[str] = BOOKS) -> str:
    rows = [
        ("Processed", "Pinnacle matches processed"),
        *((book, f"Found on {book}") for book in BOOKS if book in books),
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


def overview_tile(
    label: str, value: str, help_text: str, css: str = "", attributes: str = ""
) -> str:
    style = f' style="{css}"' if css else ""

    return (
        f'<div class="overview-tile" title="{html.escape(help_text)}">'
        f'<div class="value"{style}{attributes}>{html.escape(value)}</div>'
        f'<div class="label">{html.escape(label)}</div></div>'
    )


def overview_positive(stats: dict, books: list[str] = BOOKS) -> str:
    """Colours follow the table: amber above 0, green from the threshold up."""

    def hit(value: str, css: str) -> str:
        return css.split(";")[0] if int(value) else ""

    threshold = next(label for label in stats if label.startswith("EV(ref) >= "))
    limit = threshold.removeprefix("EV(ref) >= ")
    tiles = []

    if "Betclic" in books:
        tiles += [
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

    if "Superbet" in books and "Superbet EV(ref) > 0%" in stats:
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


def pinnacle_age_text(age: float) -> str:
    """Seconds stop mattering once the prices are stale. TABLE_SCRIPT's ageText keeps
    this format going between server ticks."""
    seconds = max(0, round(age))

    if STALE_MINUTES * 60 < seconds < 3600:
        return f"{seconds // 60} min"

    return fmt_age(seconds)


def overview_checks(summary: dict, stats: dict, books: list[str] = BOOKS) -> str:
    """Each book's gap to Pinnacle and what needs attention, in amber. Pinnacle's age
    is in the session bar."""

    def warning(label: str, value: str, count: int, help_text: str) -> str:
        return overview_tile(label, value, help_text, EV_AMBER if count else "")

    def gap(book: str) -> str:
        seconds = summary[f"{book.lower()}_gap"]

        if seconds is None:
            value = "--"
        else:
            value = f"{seconds:.1f}s" if seconds < 60 else fmt_gap(seconds)

        return overview_tile(
            f"{book} gap",
            value,
            f"Median time from the Pinnacle capture to the {book} capture, per match",
        )

    age = summary["pinnacle_age"]
    stale = age is not None and age > STALE_MINUTES * 60
    stale_text = "--" if age is None else "Yes" if stale else "No"
    # Green while fresh, red once stale, like the age in the session bar. TABLE_SCRIPT
    # flips both when the age passes the limit, between server ticks.
    stale_tile = overview_tile(
        f"Stale (over {STALE_MINUTES:g} min)",
        stale_text,
        f"Whether the Pinnacle odds are more than {STALE_MINUTES:g} minutes old right now",
        attributes="" if age is None else ' data-stale-flag=""',
    )
    if age is not None:
        stale_tile = stale_tile.replace(
            'class="value"', f'class="value {"stale" if stale else "fresh"}"', 1
        )
    tiles = [*(gap(book) for book in BOOKS if book in books), stale_tile]

    # Only in the stats when it matters: see session_summary. They are Betclic's.
    if "Errors" in stats and "Betclic" in books:
        errors = stats["Errors"]
        tiles.append(warning("Errors", errors, int(errors), "Matches that could not be checked"))

    return f'<div class="overview-tiles">{"".join(tiles)}</div>'


def session_bar_html(summary: dict, session_id: str) -> str:
    """The session's state above the overview: status, start (to the minute) and the
    age of its oldest Pinnacle prices, green while fresh and red once stale, which
    TABLE_SCRIPT counts on each second."""
    colour = STATUS_COLOURS.get(summary["status"], OVERVIEW_BLUE)
    started = session_started(session_id)
    run_on = session_id if started is None else started.strftime("%Y-%m-%d %H:%M")
    age = summary.get("pinnacle_age")
    age_html = ""

    if age is not None:
        freshness = "stale" if age > STALE_MINUTES * 60 else "fresh"
        age_html = (
            f'<span class="session-age {freshness}" title="Time since the Pinnacle odds were captured,'
            ' up to now: the oldest prices in the run">'
            f'<span data-since="{summary["pinnacle_captured"]}"'
            f' data-stale-after="{STALE_MINUTES * 60:g}">{pinnacle_age_text(age)}</span>'
            " ago</span>"
        )

    return (
        '<div class="session-bar">'
        f'<span class="session-status" style="--status: {colour}">'
        f"{html.escape(summary['status'].capitalize())}</span>"
        '<span class="session-fact"><span class="label">Run on</span>'
        f'<span class="value">{html.escape(run_on)}</span></span>'
        f"{age_html}</div>"
    )


def overview_html(
    summary: dict,
    ev_only: bool = False,
    ev_stats: dict | None = None,
    books: list[str] = BOOKS,
) -> str:
    """The stats of session_summary as two rows of cards. Bars, not pies: the counts are
    often close together (99 and 105 of 110), which pie slices do not show. ev_only
    (the EV filter is on) keeps only the Positive EV card, as narrow as its tiles,
    counted over ev_stats (all sessions) when given."""
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

    if ev_only:
        where = "" if ev_stats is None else ", all sessions"
        positive = card(
            f"Positive EV ({REFERENCE_METHOD}{where})",
            overview_positive(stats if ev_stats is None else ev_stats, books),
            "fit",
        )

        return (
            '<section class="overview"><div class="overview-title">Overview</div>'
            f'<div class="overview-cards">{positive}</div></section>'
        )

    coverage = [
        card("Pinnacle matches covered", overview_meters(stats, total, books), "wide")
    ]

    if "Betclic" in books:
        coverage.append(
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
            )
        )

    if "Superbet" in books and "Found on Superbet" in stats:
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
        card(f"Positive EV ({REFERENCE_METHOD})", overview_positive(stats, books), "wide"),
        card("Performance", overview_checks(summary, stats, books), "wide"),
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
    "CLV": "Closing line value: the price behind Best EV x Pinnacle's power-devigged "
    "probability at its last price before the start - 1. Only once the match has "
    "started; hover a value for how close to the start that price was.",
    "Result": "Whether the Bet on player won (Matchstat results). Pending until the "
    "match is played and the results cover its day; retirements and walkovers are named.",
    "Session": "The run every value in the row is from. With the EV filter over all "
    "sessions, the latest run that priced this match at or above the minimum EV.",
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
    st: Any,
    session_id: str,
    table_session: str,
    rows: list[dict],
    min_ev: float,
    ev_only: bool = False,
    ev_stats: dict | None = None,
    books: list[str] = BOOKS,
) -> None:
    """Runs every second, so it stays cheap. rows belong to table_session."""
    info = session_info(session_id)
    now = datetime.now(timezone.utc)
    waiting = table_session != session_id
    summary = session_summary([] if waiting else rows, info, min_ev, now)

    st.html(session_bar_html(summary, session_id))

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

    st.html(overview_html(summary, ev_only, ev_stats, books))

    if waiting:
        age = session_summary(rows, {}, min_ev, now)["pinnacle_age"]
        prices = f", its Pinnacle odds {fmt_age(age)} old" if age is not None else ""

        st.warning(
            "Waiting for this run's first match. The table still shows the previous "
            f"run ({session_label(table_session)}{prices})."
        )


def empty_message(records: list[dict], filters: dict) -> str:
    """Says how close the sessions came when the EV filter emptied the table."""
    if filters.get("player_all"):
        return f"No session has a match with a player matching '{filters['player'].strip()}'."

    if not filters["with_ev"]:
        return "No match fits the filters."

    best = max((r["Best EV"] for r in records if r["Best EV"] is not None), default=None)
    limit = f"{filters['min_ev']:.1%}"
    where = "any session" if filters.get("all_sessions") else "this session"

    if best is None:
        return f"No match in {where} has an EV of at least {limit}: none has an EV yet."

    return f"No match in {where} has an EV of at least {limit}. The best is {best:+.1%}."


def render_table(
    box: Any,
    session_id: str,
    rows: list[dict],
    filters: dict,
    all_records: list[dict] | None = None,
    outcomes: dict | None = None,
) -> None:
    """all_records (every session's) replaces this session's rows when given;
    outcomes (load_outcome_sources) adds the CLV and Result columns asked for."""
    min_ev = filters["min_ev"]
    books = filters.get("books", BOOKS)
    records = table_records(rows, books) if all_records is None else all_records
    candidates = table_candidates(records, all_records, filters)
    shown = filter_records(
        candidates,
        filters["with_ev"],
        min_ev,
        filters["tours"],
        filters["draws"],
        filters["player"],
    )

    if all_records is None:
        box.caption(f"Showing {len(shown)} of {len(records)} matches")
    elif filters.get("player_all"):
        sessions = len({record["Session"] for record in shown})
        box.caption(
            f"Showing {len(shown)} appearances in {sessions} "
            f"session{'s' * (sessions != 1)}, newest first"
        )
    else:
        box.caption(
            f"Showing {len(shown)} matches from all sessions, each from the latest "
            "run that reached the minimum EV, so older prices may have moved"
        )

    if not shown:
        waiting = is_running(session_id) and not records
        box.info("Waiting for the first match..." if waiting else empty_message(records, filters))
        return

    superbet = "Superbet" in books and (
        has_superbet(rows)
        if all_records is None
        else any(record["EV Superbet"] is not None for record in shown)
    )
    if outcomes is not None:
        shown = add_outcomes(
            shown,
            outcomes,
            datetime.now(timezone.utc),
            clv=filters["show_clv"],
            result=filters["show_result"],
        )

    box.html(table_html(shown, min_ev, superbet, "Betclic" in books))


PICKER_STYLE = """<style>
.st-key-session_picker [data-testid="stPopoverButton"] > div {
    width: 100%;
    justify-content: space-between;
}
.st-key-session_picker [data-testid="stPopoverButton"] > div > div:first-child {
    flex: 1;
    justify-content: flex-start;
}
.st-key-session_calendar [data-testid="stHorizontalBlock"],
.st-key-day_sessions [data-testid="stHorizontalBlock"] {
    gap: 0.2rem;
}
.st-key-session_calendar [data-testid="stColumn"],
.st-key-day_sessions [data-testid="stColumn"] {
    min-width: 0;
}
.st-key-session_calendar button {
    min-height: 2.1rem;
    min-width: 2.4rem;
    padding: 0;
}
.st-key-session_calendar .weekday {
    text-align: center;
    font-size: 0.8rem;
    opacity: 0.6;
}
.st-key-session_calendar .month {
    text-align: center;
    font-weight: 600;
    line-height: 2.1rem;
}
</style>"""


PILLS_STYLE = """<style>
.st-key-books [data-testid="stBaseButton-pillsActive"]::before,
.st-key-tours [data-testid="stBaseButton-pillsActive"]::before,
.st-key-draws [data-testid="stBaseButton-pillsActive"]::before {
    content: "✓";
    font-weight: 700;
    margin-right: 0.35rem;
}
</style>"""


def pill_choice(previous: list[str], chosen: list[str]) -> list[str]:
    """What a Tour or Draw pill click leaves selected, given the selection before it
    (which always is [ALL_PILL] or a list without it)."""
    if not chosen:
        return [ALL_PILL]

    if ALL_PILL in chosen and ALL_PILL not in previous:
        return [ALL_PILL]

    return [value for value in chosen if value != ALL_PILL] or [ALL_PILL]


def pill_filter(chosen: list[str]) -> list[str] | None:
    """None (no restriction) while All is picked, as filter_records expects."""
    return None if not chosen or ALL_PILL in chosen else list(chosen)


def previous_key(key: str) -> str:
    return f"{key}_previous"


def on_pill_change(state: Any, key: str) -> None:
    state[key] = pill_choice(state.get(previous_key(key), [ALL_PILL]), state[key] or [])


def all_pills(st: Any, label: str, options: list[str], key: str) -> list[str] | None:
    """Multi-select pills with All first. Its selection before a click is kept under
    previous_key, written on every run, so a reset to the defaults stays in step."""
    chosen = st.pills(
        label,
        [ALL_PILL, *options],
        selection_mode="multi",
        key=key,
        on_change=on_pill_change,
        args=(st.session_state, key),
    )
    st.session_state[previous_key(key)] = list(chosen or [])

    return pill_filter(chosen)


def book_choice(previous: list[str], chosen: list[str]) -> list[str]:
    """Turning off the last book is refused: the page always shows one."""
    return [book for book in BOOKS if book in chosen] or list(previous or BOOKS)


def on_book_change(state: Any) -> None:
    state["books"] = book_choice(state.get(previous_key("books"), BOOKS), state["books"] or [])


def book_pills(st: Any) -> list[str]:
    """Betclic and Superbet, each on or off; always at least one on."""
    chosen = st.pills(
        "Bookmakers",
        BOOKS,
        selection_mode="multi",
        key="books",
        on_change=on_book_change,
        args=(st.session_state,),
        help="Turn a book off to hide its columns and overview cards; Bet on and "
        "Best EV then come from the other book only. One always stays on.",
    )
    chosen = [book for book in BOOKS if book in (chosen or [])] or list(BOOKS)
    st.session_state[previous_key("books")] = chosen

    return chosen


def picker_label(selected: str) -> str:
    return selected if selected == LATEST else session_label(selected)


# Marks on a session's time; a finished one has none.
STATUS_MARKS = {"running": " ●", "failed": " ✕", "interrupted": " !"}


def session_button(session_id: str, status: str) -> tuple[str, str]:
    """The button text (start time and a status mark) and its tooltip."""
    started = session_started(session_id)
    clock = started.strftime("%H:%M") if started else session_id
    seconds = started.strftime("%H:%M:%S") if started else session_id

    return clock + STATUS_MARKS.get(status, ""), f"{seconds} · {status}"


def reset_settings(state: Any) -> None:
    """Every sidebar setting back to its default. It runs in a button callback, so
    before the widgets are drawn, which is when Streamlit allows setting their state."""
    for key, value in SETTING_DEFAULTS.items():
        state[key] = list(value) if isinstance(value, list) else value

    state[RESET_KEY] = state.get(RESET_KEY, 0) + 1


def reset_script(count: int) -> str:
    """Resets the table's switches in the browser. The count makes each reset's
    HTML differ from the last, so Streamlit sends and runs it again."""
    return (f"<script>/* reset {count} */ "
            "window.resetMatchesTable && window.resetMatchesTable();</script>")


def choose_session(st: Any, value: str) -> None:
    if value == LATEST:
        reset_settings(st.session_state)

    st.session_state[SESSION_KEY] = value
    st.session_state[PICKER_KEY] = False
    # Closing the panel hides the button that was clicked, so the rerun of the
    # whole page cannot wait for that button to return True.
    st.session_state[CHOSEN_KEY] = True


def render_session_picker(st: Any) -> None:
    """A button that opens a month calendar. Days with a run are buttons; clicking
    one lists its sessions under the calendar, and clicking a session picks it and
    closes the panel. It runs as a fragment, so moving through months and days
    does not rebuild the table; only picking a session does."""
    state = st.session_state

    if state.pop(CHOSEN_KEY, False):
        st.rerun(scope="app")

    sessions = list_sessions()
    days = sessions_by_day(sessions)
    selected = state.setdefault(SESSION_KEY, LATEST)

    if selected != LATEST and selected not in sessions:
        selected = state[SESSION_KEY] = LATEST

    if DAY_KEY not in state:
        shown = selected if selected != LATEST else (sessions[0] if sessions else None)
        started = session_started(shown) if shown else None
        state[DAY_KEY] = started.date() if started else None
        state[MONTH_KEY] = (started.date() if started else date.today()).replace(day=1)

    st.markdown("Session")
    panel = st.popover(picker_label(selected), width="stretch", key=PICKER_KEY, on_change="rerun")

    if not panel.open:
        return

    with panel:
        st.button(
            LATEST,
            type="primary" if selected == LATEST else "secondary",
            width="stretch",
            on_click=choose_session,
            args=(st, LATEST),
        )

        if not days:
            st.caption("No sessions yet.")
            return

        first_month = min(days).replace(day=1)
        last_month = max(max(days), date.today()).replace(day=1)
        month = min(max(state[MONTH_KEY], first_month), last_month)

        def move(step: int) -> None:
            state[MONTH_KEY] = shift_month(month, step)

        def pick_day(day: date) -> None:
            state[DAY_KEY] = day

        with st.container(key="session_calendar"):
            back, title, forward = st.columns([1, 5, 1], vertical_alignment="center")
            back.button("‹", key="month_back", on_click=move, args=(-1,),
                        disabled=month <= first_month, width="stretch")
            title.html(f'<div class="month">{month.strftime("%B %Y")}</div>')
            forward.button("›", key="month_forward", on_click=move, args=(1,),
                           disabled=month >= last_month, width="stretch")

            for column, name in zip(st.columns(7), WEEKDAYS):
                column.html(f'<div class="weekday">{name}</div>')

            for week in month_weeks(month):
                for column, day in zip(st.columns(7), week):
                    if day is None:
                        continue

                    runs = days.get(day, [])
                    column.button(
                        str(day.day),
                        key=f"day_{day.isoformat()}",
                        type="primary" if day == state[DAY_KEY] else
                        "secondary" if runs else "tertiary",
                        disabled=not runs,
                        help=f"{len(runs)} session{'s' * (len(runs) != 1)}" if runs else None,
                        on_click=pick_day,
                        args=(day,),
                        width="stretch",
                    )

        day = state[DAY_KEY]
        runs = days.get(day, []) if day else []

        if not runs:
            st.caption("Pick a highlighted day to see its sessions.")
            return

        statuses = {
            session_id: str(session_info(session_id).get("status", "running")).lower()
            for session_id in runs
        }
        legend = "".join(
            f" ·{mark} {status}"
            for status, mark in STATUS_MARKS.items()
            if status in statuses.values()
        )
        st.caption(f"{day.strftime('%a %d %b %Y')}: {len(runs)} "
                   f"session{'s' * (len(runs) != 1)}{legend}")

        with st.container(key="day_sessions"):
            for start in range(0, len(runs), SESSIONS_PER_ROW):
                row = runs[start:start + SESSIONS_PER_ROW]

                for column, session_id in zip(st.columns(SESSIONS_PER_ROW), row):
                    status = statuses[session_id]
                    text, tooltip = session_button(session_id, status)
                    column.button(
                        text,
                        key=f"pick_{session_id}",
                        help=tooltip,
                        type="primary" if session_id == selected else "secondary",
                        width="stretch",
                        on_click=choose_session,
                        args=(st, session_id),
                    )


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


def keep_running() -> bool:
    return os.environ.get(KEEP_RUNNING_ENV, "").strip() == "1"


def start_browser_watch() -> None:
    if keep_running():
        return

    threading.Thread(target=stop_server_when_browsers_close, daemon=True).start()


# The About page is this same app with ?page=about, so the sidebar link can open it
# in a new tab (st.link_button always does) without a second Streamlit page.
ABOUT_PAGE = "about"
ABOUT_URL = f"?page={ABOUT_PAGE}"
METHODOLOGY = ROOT / "docs" / "methodology.md"
IMAGE_LINE = re.compile(r"^!\[(?P<alt>[^\]]*)\]\((?P<path>[^)]+)\)\s*$")

ABOUT_INTRO = """
# Tennis EV

This project looks for **positive expected value (EV)** bets on tennis match winners.

Pinnacle is a sharp bookmaker: its prices are taken as the best available estimate of
each player's real chance. Its margin is removed (*devigging*) to turn the odds into
fair probabilities, and those are compared with the prices of soft bookmakers
(Betclic and Superbet). A price is worth betting when

$$
\\text{EV} = p_{\\text{fair}} \\times \\text{odds} - 1 > 0 .
$$

There are two parts:

* **Live pipeline.** Each `scripts/run_once.py` run is one *session*: one Pinnacle
  snapshot, the soft bookmakers' prices for the same matches, and the EV of every
  side. This dashboard reads those session files and writes nothing.
* **Historical evaluation.** Several devig methods were compared on Pinnacle closing
  odds of past matches, to choose the one the live pipeline ranks by (Power). The
  full method and results are below.

Nothing here is betting advice: the EV is only as good as the assumption that
Pinnacle's price is fair, and it is measured against results as they come in
(the **Result** and **CLV** columns).

## Reading the dashboard

* **Session**: *Latest* follows the newest run as it is written; any past run can
  be picked from the calendar.
* **Bet on**: the side with the better EV, priced at whichever bookmaker gives the
  higher EV. **Best EV** uses the reference method (Power); `EV(OR)`, `EV(shin)` and
  `EV(prop)` show the other devig methods, so you can see whether they agree.
* **Superbet prices** are shown as in the app; its payouts are taxed (0.88 x stake x
  odds), so its EV is always after tax. Betclic prices already include the tax.
* **Show all matches with EV**: only matches with best EV at or above the minimum,
  in this session or across all sessions (one row per match, newest sighting).
* **Result**: Won or Lost for the Bet on side, once the match results include it.
* **CLV** (closing line value): the price taken against Pinnacle's last fair price
  before the start. Consistently positive CLV is the usual sign that an edge is real.
* **Session bar**: the run's status, when it started and how long ago
  its oldest Pinnacle prices were captured (green, red once over 15 minutes).
* **Performance**: how long after the Pinnacle prices each bookmaker's prices were
  read, and whether the Pinnacle prices are stale (over 15 minutes old).
"""


def about_parts(text: str) -> list[tuple[str, str]]:
    """Markdown cut around its image lines, as ("markdown", text) and ("image", path)
    parts: st.markdown cannot load a local image, so they go through st.image.
    Image paths are relative to docs/, as in docs/methodology.md."""
    parts: list[tuple[str, str]] = []
    lines: list[str] = []
    for line in text.splitlines():
        image = IMAGE_LINE.match(line.strip())
        if image is None:
            lines.append(line)
            continue
        if "".join(lines).strip():
            parts.append(("markdown", "\n".join(lines)))
        lines = []
        parts.append(("image", image["path"]))
    if "".join(lines).strip():
        parts.append(("markdown", "\n".join(lines)))
    return parts


def render_about(st) -> None:
    st.markdown(ABOUT_INTRO)
    st.divider()
    try:
        methodology = METHODOLOGY.read_text(encoding="utf-8")
    except OSError:
        st.info(f"{METHODOLOGY.relative_to(ROOT)} not found.")
        return
    for kind, value in about_parts(methodology):
        if kind == "markdown":
            st.markdown(value)
            continue
        image = (METHODOLOGY.parent / value).resolve()
        if image.is_file():
            st.image(str(image))
        else:
            st.caption(f"Missing figure: {value}")


def main() -> None:
    import streamlit as st

    if st.query_params.get("page") == ABOUT_PAGE:
        st.set_page_config(page_title="About · Tennis EV", layout="centered")
        # Cached so that the whole server, across tabs and reruns, starts one watcher.
        st.cache_resource(show_spinner=False)(start_browser_watch)()
        render_about(st)
        return

    st.set_page_config(page_title="Tennis EV", layout="wide")
    st.html(
        NO_STALE_FADE
        + TABLE_STYLE
        + OVERVIEW_STYLE
        + PICKER_STYLE
        + PILLS_STYLE
        + TABLE_SCRIPT,
        unsafe_allow_javascript=True,
    )

    # Cached so that the whole server, across tabs and reruns, starts one watcher.
    st.cache_resource(show_spinner=False)(start_browser_watch)()

    state = st.session_state
    # The widgets take their defaults from Session State, so reset_settings can put
    # them back; giving a widget a default as well would make Streamlit warn.
    for key, value in SETTING_DEFAULTS.items():
        if key not in state:
            state[key] = list(value) if isinstance(value, list) else value

    with st.sidebar:
        player = st.text_input("Player", key="player")
        # Disabled, it still returns its value, so filters checks the name as well.
        player_all = st.checkbox(
            "Search all sessions",
            key="player_all",
            disabled=not player.strip(),
            help="Every appearance of this player in every session, one row per "
            "session, newest first. The other filters still apply.",
        )
        st.fragment(render_session_picker)(st)
        selected = state[SESSION_KEY]

        resets = state.get(RESET_KEY, 0)
        if resets != state.get("settings_reset_sent", 0):
            state["settings_reset_sent"] = resets
            st.html(reset_script(resets), unsafe_allow_javascript=True)

        with st.container(border=True):
            with_ev = st.checkbox(
                "Show all matches with EV",
                key="with_ev",
                help="Only matches whose best EV is at least the minimum below. "
                "The overview then keeps only its Positive EV card.",
            )
            scope = st.radio(
                "Sessions",
                [SCOPE_SESSION, SCOPE_ALL],
                key="ev_scope",
                horizontal=True,
                disabled=not with_ev,
                label_visibility="collapsed",
            )
            # Disabled, it still returns its value, which colours the EV cells.
            threshold = st.slider(
                "Minimum EV",
                min_value=0.0,
                max_value=MAX_MIN_EV_PERCENT,
                key="min_ev",
                step=0.1,
                format="%.1f%%",
                disabled=not with_ev,
                help="Green from here up.",
            )
            show_result = st.checkbox(
                "Show result",
                key="show_result",
                help="Won (green) or Lost (red) for the Bet on player, once the "
                "match results include it.",
            )
            show_clv = st.checkbox(
                "Show CLV",
                key="show_clv",
                help="Against Pinnacle's last price before the start, once the match "
                "has started.",
            )
        books = book_pills(st)
        tours = all_pills(st, "Tour", TOURS, "tours")
        draws = all_pills(st, "Draw", DRAWS, "draws")
        st.write("")
        st.link_button(
            "About this project",
            ABOUT_URL,
            icon=":material/info:",
            width="stretch",
            help="What the project does, how to read this table and how the devig "
            "method was chosen. Opens in a new tab.",
        )

    filters = {
        "with_ev": with_ev,
        "all_sessions": with_ev and scope == SCOPE_ALL,
        "show_clv": show_clv,
        "show_result": show_result,
        "min_ev": threshold / 100,
        "books": books,
        "tours": tours,
        "draws": draws,
        "player": player,
        "player_all": player_all and bool(player.strip()),
    }

    header_box = st.container()
    table_box = st.container()

    first_header, table_session = current_sessions(selected)
    running = bool(first_header) and is_running(first_header)
    rows, drawn = load_rows(st, table_session) if table_session else ([], None)

    all_records, ev_stats = None, None
    if filters["all_sessions"] or filters["player_all"]:
        cache = st.cache_resource(show_spinner=False)(records_cache)()
        with st.spinner("Reading every session..."):
            all_records = all_session_records(list_sessions(), cache, books)
    if filters["all_sessions"]:
        ev_stats = positive_ev_stats(all_records, filters["min_ev"])

    outcomes = None
    if show_clv or show_result:
        cache = st.cache_resource(show_spinner=False)(outcomes_cache)()
        with st.spinner("Reading Pinnacle snapshots and match results..."):
            outcomes = load_outcome_sources(cache)

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
            render_header(
                st,
                header_session,
                newest_table,
                fresh,
                filters["min_ev"],
                filters["with_ev"],
                ev_stats,
                books,
            )

    # Header first: it is quick, and the table can take a second or two.
    with header_box:
        live_header()

    if table_session:
        render_table(table_box, table_session, rows, filters, all_records, outcomes)
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
