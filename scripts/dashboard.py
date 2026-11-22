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
import csv
import html
import os
import pickle
import re
import statistics
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
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
    SUPERBET_RAW_DIR,
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
from results_store import (  # noqa: E402
    BETCLIC_MIN_ODDS,
    METHODS,
    REFERENCE_METHOD,
    ev_column,
    overround,
)

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
    "Alerted",
}

# Files behind the CLV and Result columns: Pinnacle's snapshots (every match, so
# also prices after the last session) and the finished matches.
PINNACLE_RAW_DIR = ROOT / "data" / "pinnacle" / "raw"
# PinnFeed's free plan (since 20 Nov 2026), for the Pinnacle box's tip when a run's
# status lacks the reply's headers: 1,000 requests a day for the account, prices
# 30 s behind Pinnacle. Tennis makes one request per 5-minute run, 288 a day.
PINNFEED_DAILY_LIMIT = 1000
PINNFEED_SITE = "https://pinnfeed.io"
PINNFEED_DELAY_SECONDS = 30
TENNIS_RUNS_PER_DAY = 288
MATCH_RESULTS_FILES = [
    ROOT / "data" / "match_results" / "results.csv",
    ROOT / "data" / "match_results" / "state.json",
]
# Results from optional private collectors (match_results.EXTRA_DIR).
MATCH_RESULTS_EXTRA_DIR = ROOT / "data" / "match_results" / "extra"

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
/* A Telegram alert was sent on this player (user, 20 Nov 2026: replaces the Alerted
   column). A gradient over the background colour keeps the sticky first column opaque. */
table.matches td.alerted {
    background-image: linear-gradient(rgba(201, 138, 0, 0.2), rgba(201, 138, 0, 0.2));
    cursor: help;
}
table.matches td.alerted-other { cursor: help; }
table.matches td .bell { margin-right: 6px; font-size: 12px; }
table.matches td.alerted-other .bell { opacity: 0.45; filter: grayscale(1); }
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
/* Phones get one card per match instead of the table (user, 10 Nov 2026): the sticky
   name column took half the screen and left room for one more column. */
.match-cards { display: none; }
@media (max-width: 700px) {
    table.matches { display: none; }
    .match-cards { display: flex; flex-direction: column; gap: 8px; }
    /* Opaque, so the sidebar button does not sit on top of the scrolled cards. */
    [data-testid="stHeader"] { background: var(--page-background) !important; }
    [data-testid="stMainBlockContainer"] { padding-top: 3.5rem !important; }
}
.match-card {
    padding: 10px 12px;
    border: 1px solid rgba(128, 128, 128, 0.25);
    border-radius: 10px;
    font-size: 14px;
    line-height: 20px;
}
.match-card.dimmed { opacity: 0.6; }
.mc-head { display: flex; align-items: flex-start; gap: 10px; }
.mc-names { flex: 1; min-width: 0; }
.mc-bet { font-size: 16px; font-weight: 600; line-height: 22px; }
.mc-vs, .mc-meta, .mc-book .label, .mc-book .sub {
    color: color-mix(in srgb, currentColor 65%, transparent);
}
.mc-vs, .mc-meta { font-size: 13px; }
.mc-meta { margin-top: 2px; }
.mc-best {
    flex: none;
    padding: 2px 8px;
    border-radius: 6px;
    font-size: 18px;
    line-height: 26px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
}
.mc-books {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(90px, 1fr));
    gap: 6px;
    margin-top: 8px;
}
.mc-book {
    padding: 5px 8px;
    border-radius: 6px;
    background-color: rgba(128, 128, 128, 0.1);
    font-variant-numeric: tabular-nums;
}
.mc-book .label, .mc-book .sub { font-size: 12px; line-height: 16px; }
.mc-book .price { font-size: 16px; font-weight: 600; }
.mc-book .ev { margin-left: -3px; padding: 0 3px; border-radius: 4px; font-weight: 600; }
.mc-extra {
    display: flex;
    flex-wrap: wrap;
    gap: 2px 14px;
    margin-top: 8px;
    font-size: 13px;
}
.mc-extra .label {
    margin-right: 4px;
    color: color-mix(in srgb, currentColor 65%, transparent);
}
</style>"""

# One horizontal row of cards; on a narrow window they wrap. Same colours in both themes:
# they hold up on light and dark, and every bar has its numbers printed beside it.
OVERVIEW_BLUE = "#2a78d6"
OVERVIEW_NO_EV = "#c98a00"
SUPERBET_NO_EV_LABEL = "FOUND on Superbet but no EV"
OVERVIEW_NOT_MATCHED = "rgba(128, 128, 128, 0.65)"
OVERVIEW_WAITING = "rgba(128, 128, 128, 0.2)"
OVERVIEW_STARTED = "rgba(160, 120, 200, 0.55)"
STATUS_COLOURS = {"finished": "#1f9d55", "failed": "#d64545", "interrupted": OVERVIEW_NO_EV}
# The pill's words (user, 22 Nov 2026): a finished run whose book failed is Failed too.
STATUS_LABELS = {"finished": "Successful", "failed": "Failed"}
# Sources that make a finished run Failed; the markets are comparison only.
PILL_SOURCES = {"betclic": "Betclic", "superbet": "Superbet"}
OVERVIEW_STYLE = """<style>
/* The run's heading (user, 15 Nov 2026): "Last run overview" with the status pill on
   its right, then its facts as values over small grey labels. */
/* Equal space over and under the facts (user, 16 Nov 2026): 16 px each, the gap
   Streamlit leaves between this and the overview below. */
.session-head { margin-bottom: 0; }
.session-title { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; }
.session-title .overview-title { margin-bottom: 0; }
.session-facts { display: flex; flex-wrap: wrap; gap: 4px 28px; margin-top: 16px; }
.session-facts .value {
    font-size: 17px;
    line-height: 22px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
}
.session-facts .label {
    font-size: 12px;
    line-height: 16px;
    color: color-mix(in srgb, currentColor 60%, transparent);
}
.session-status {
    font-size: 14px;
    line-height: 24px;
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
.flow {
    display: flex;
    flex-wrap: wrap;
    align-items: stretch;
    gap: 6px 0;
    margin: 2px 0 10px;
}
/* Boxes of one size (user, 15 Nov 2026), the name centred in each. */
.flow-node {
    flex: 1 1 0;
    min-width: 0;
    max-width: 150px;
    display: flex;
    flex-direction: column;
    justify-content: center;
    text-align: center;
    white-space: nowrap;
    padding: 6px 8px;
    border: 1px solid rgba(128, 128, 128, 0.35);
    border-radius: 6px;
    background-color: rgba(128, 128, 128, 0.08);
}
/* Only the Prediction markets tab lays steps out; here they are not there at all. */
.flow-step { display: contents; }
.flow-name { font-weight: 600; }
.flow-node .tip { white-space: normal; text-align: left; font-weight: 400; }
.flow-sub { font-size: 12px; font-variant-numeric: tabular-nums; font-weight: 600; }
.flow-arrow {
    flex: none;
    align-self: center;
    display: flex;
    flex-direction: column;
    align-items: center;
    min-width: 62px;
    padding: 0 6px;
}
/* An empty line under the arrow as tall as the number over it, so the arrow itself
   sits at the boxes' middle. */
.flow-arrow::after { content: ""; height: 18px; }
.flow-gap { font-size: 14px; line-height: 18px; font-variant-numeric: tabular-nums; }
/* A line with an arrowhead, in the muted text colour; the head is inside the line's
   box, so the number is centred over the whole arrow. */
.flow-line {
    position: relative;
    align-self: stretch;
    height: 2px;
    margin: 3px 8px 3px 0;
    background-color: color-mix(in srgb, currentColor 50%, transparent);
}
.flow-line::after {
    content: "";
    position: absolute;
    right: -8px;
    top: -5px;
    border: 6px solid transparent;
    border-left: 8px solid color-mix(in srgb, currentColor 50%, transparent);
    border-right: 0;
}
/* One line on a phone (user, 10 Nov 2026): no "Run on" label or year, tighter gaps. */
@media (max-width: 700px) {
    /* The flow on one line down to about 360 px wide. */
    .flow-node { max-width: none; padding: 6px 4px; font-size: 14px; }
    .flow-sub { font-size: 11px; }
    .flow-gap { font-size: 13px; }
    .flow-arrow { min-width: 46px; padding: 0 4px; }
    .session-status { padding: 0 9px; }
    .session-facts .year { display: none; }
}
.fresh { color: #1f9d55; }
.stale { color: #d64545; }
.overview { margin-bottom: 4px; }
/* Section headings: Last run overview, Statistics (all runs). */
.overview-title { font-weight: 600; font-size: 20px; line-height: 28px; margin-bottom: 6px; }
.overview-cards { display: flex; flex-wrap: wrap; gap: 10px; }
.overview-cards + .overview-cards { margin-top: 10px; }
.overview-cards + .overview-title { margin-top: 14px; }
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
.donut-pair { display: flex; flex-wrap: wrap; gap: 10px 24px; }
.donut-block { flex: 1 1 0; min-width: 0; }
.donut-book { font-weight: 600; margin-bottom: 4px; }
/* Ring and counts side by side; on a narrow card the counts wrap under the ring. */
.donut-body { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 14px; }
.donut { position: relative; width: 92px; height: 92px; flex: none; }
.donut-ring {
    position: absolute;
    inset: 0;
    border-radius: 50%;
    -webkit-mask: radial-gradient(farthest-side, transparent calc(100% - 12.5px), #000 calc(100% - 12px));
    mask: radial-gradient(farthest-side, transparent calc(100% - 12.5px), #000 calc(100% - 12px));
}
.donut-value {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 19px;
    font-weight: 600;
}
.donut-whole { font-size: 13px; font-weight: 400; opacity: 0.65; }
.donut-legend { line-height: 1.5; white-space: nowrap; }
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
.has-tip { position: relative; cursor: help; outline: none; }
.has-tip .tip {
    display: none;
    position: absolute;
    left: 0;
    top: calc(100% + 4px);
    z-index: 5;
    width: max-content;
    max-width: 260px;
    padding: 4px 8px;
    border-radius: 6px;
    font-size: 12px;
    line-height: 16px;
    color: #fff;
    background: #333;
}
.has-tip:hover .tip, .has-tip:focus .tip { display: block; }
/* The tip stays open while the mouse crosses the gap to it, so its text can be
   selected and copied (user, 20 Nov 2026). */
.has-tip .tip { cursor: text; user-select: text; }
.has-tip .tip a { color: inherit; text-decoration: underline; cursor: pointer; }
.has-tip .tip::before { content: ""; position: absolute; left: 0; right: 0; top: -6px; height: 6px; }
/* Dotted and grey (user, 15 Nov 2026): a hint that a tap explains it, quieter than the text. */
.has-tip .label {
    text-decoration: underline dotted;
    text-decoration-color: color-mix(in srgb, currentColor 40%, transparent);
    text-underline-offset: 3px;
}
@media (max-width: 700px) {
    /* Over the page, not inside the tile. The Track record's tiles sit in a 3-column
       grid, so a tip in the right column (.end, Lasts) opens leftwards from the tile's
       right edge and stays on screen. */
    .has-tip .tip { max-width: 220px; }
    .has-tip.end .tip { left: auto; right: 0; }
}
.overview-tile .label {
    font-size: 12px;
    line-height: 16px;
    color: color-mix(in srgb, currentColor 65%, transparent);
}
.track-rows {
    display: grid;
    grid-template-columns: auto 1fr;
    align-items: center;
    gap: 6px 16px;
}
.track-book { font-weight: 600; }
.track-note {
    font-size: 12px;
    color: color-mix(in srgb, currentColor 65%, transparent);
}
/* Phones (user, 11 Nov 2026: the wrapped tiles blended together): each book gets a
   header line over its own grid of boxed tiles, three a row, so every value keeps its label. */
@media (max-width: 700px) {
    .track-rows { display: block; }
    .track-book {
        margin: 12px 0 6px;
        padding-bottom: 4px;
        border-bottom: 1px solid rgba(128, 128, 128, 0.3);
        font-size: 14px;
    }
    .track-book:first-child { margin-top: 2px; }
    .track-note { margin-top: 6px; }
    .track-rows .overview-tiles {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 6px;
    }
    .track-rows .overview-tile {
        padding: 5px 8px;
        border-radius: 6px;
        background-color: rgba(128, 128, 128, 0.1);
    }
    .track-rows .overview-tile .value { font-size: 18px; line-height: 24px; }
    .track-rows .overview-tile .label { font-size: 11px; line-height: 14px; }
}
.overview-empty { color: color-mix(in srgb, currentColor 65%, transparent); }
.overview-loading { display: flex; align-items: center; gap: 10px; }
.spinner {
    flex: none;
    width: 18px;
    height: 18px;
    box-sizing: border-box;
    border: 2px solid color-mix(in srgb, currentColor 25%, transparent);
    border-top-color: #2a78d6;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
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
        // Streamlit's sanitizer drops target="_blank", so a link in a tip (PinnFeed's
        // site) is opened in a new tab here instead of leaving the dashboard.
        const link = event.target.closest(".tip a[href]");

        if (link) {
            event.preventDefault();
            window.open(link.href, "_blank", "noopener");
            return;
        }

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
        const age = document.querySelector(".flow-age [data-since]");

        if (!age || !age.firstChild) return;

        const seconds = Math.max(0, Math.round(Date.now() / 1000 - Number(age.dataset.since)));
        const staleAfter = Number(age.dataset.staleAfter);
        const shown = ageText(seconds, staleAfter);
        const stale = seconds > staleAfter;

        if (age.firstChild.nodeValue !== shown) age.firstChild.nodeValue = shown;

        // Green while fresh, red once stale: the age in Data flow and age, and the Stale data flag.
        const flag = document.querySelector("[data-stale-flag]");
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
    """The Signals card's counts over many sessions, each match counted once."""

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
    """Changes when the results are updated. New Pinnacle snapshots are added to the
    sightings one file at a time instead (load_outcome_sources)."""
    files = []
    for path in [*MATCH_RESULTS_FILES, *sorted(MATCH_RESULTS_EXTRA_DIR.glob("*.csv"))]:
        try:
            stat = path.stat()
            files.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            files.append(None)

    return tuple(files)


# A snapshot file younger than this may still be being written, so it waits for the
# next call (snapshots are written once and never changed).
SNAPSHOT_SETTLE_SECONDS = 5
SNAPSHOT_CHUNK = 200


def load_outcome_sources(cache: dict) -> dict:
    """Pinnacle's snapshots by match (compact_sightings: 79 MB for every sighting on 15
    Nov 2026, growing by about 25 MB a day with runs every 5 minutes) and the finished
    matches. Snapshots are read once each, as they arrive: rereading all of them took
    13.5 s for 1800 files on the VM, and a new one now comes every 5 minutes. The
    results are read again only when outcome_signature changes."""
    import evaluate_ev

    known = cache.setdefault("sighting_files", set())
    settled = time.time() - SNAPSHOT_SETTLE_SECONDS
    new = []

    for path in sorted(PINNACLE_RAW_DIR.glob("*.csv")):
        if path.name in known:
            continue
        try:
            if path.stat().st_mtime > settled:
                continue
        except OSError:
            continue
        new.append(path)

    if new or "sightings" not in cache:
        # A new dict, so a reader holding the old one never sees it change.
        sightings = dict(cache.get("sightings", {}))

        # In chunks, so the first load never holds every snapshot row at once.
        for chunk in range(0, len(new), SNAPSHOT_CHUNK):
            groups: dict = {}
            touched = evaluate_ev.add_sightings(groups, new[chunk : chunk + SNAPSHOT_CHUNK])

            for key in touched:
                for row in sightings.get(key, []):
                    groups[key].setdefault(row["captured_at"], row)
                sightings[key] = evaluate_ev.compact_sightings(
                    evaluate_ev.sorted_sightings(groups[key])
                )

        known.update(path.name for path in new)
        cache["sightings"] = sightings

    signature = outcome_signature()

    if cache.get("signature") != signature:
        import match_results

        results = match_results.load_results()
        cache.update(
            signature=signature,
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


ALERT_BELL = "🔔"
ALERT_OTHER_SIDE = f"{ALERT_BELL} other side:"


def alert_time(moment: datetime | None) -> str:
    if moment is None:
        return "time unknown"

    local = moment.astimezone()
    return f"{local.day} {local:%b %H:%M}"


def alert_text(alerts: list[dict], same_side: bool) -> tuple[str, str]:
    """('🔔 Betclic 3.90, Superbet 3.75 · 10 Nov 00:15', tooltip) for one side's
    alerts, timed by the first."""
    alerts = sorted(alerts, key=lambda alert: alert["at"] or datetime.max.replace(tzinfo=timezone.utc))
    prices = ", ".join(
        f"{alert['book']} {alert['price']:.2f}" if alert["price"] else alert["book"]
        for alert in alerts
    )
    when = alert_time(alerts[0]["at"])
    prefix = ALERT_BELL if same_side else ALERT_OTHER_SIDE
    sent = "; ".join(
        f"{alert['book']}"
        + (f" {alert['price']:.2f}" if alert["price"] else "")
        + (f" (EV {alert['ev']:+.1%})" if alert["ev"] is not None else "")
        + f" at {alert_time(alert['at'])}"
        for alert in alerts
    )
    title = (
        f"Telegram alert already sent on {alerts[0]['player']}: {sent}. "
        + (
            "Treat it as already bet; a later EV on this side is the same bet again."
            if same_side
            else "That was the other player, so this row is a different bet."
        )
    )

    return f"{prefix} {prices} · {when}", title


def add_alerts(records: list[dict], alerts: list[dict]) -> list[dict]:
    """Marks matches a Telegram alert was sent for (notify.sent_alerts) with hidden
    fields, no column (user, 20 Nov 2026: the Alerted column took too much room):
    `_alert` ('🔔 Betclic 3.90 · 10 Nov 00:15', the first alert's price and time),
    `_title Alerted` (its tooltip) and `_alert_side`, the name column of the alerted
    player ('Bet on', or 'Opponent' for an alert on the other side). The table puts
    a bell on that name and tints it; phone cards keep an Alerted line."""
    by_match: dict[tuple, list[dict]] = {}

    for alert in alerts:
        key = frozenset((alert["player1"], alert["player2"])), alert["tournament"]
        by_match.setdefault(key, []).append(alert)

    out = []

    for record in records:
        found = by_match.get(match_key(record), [])

        if not found:
            out.append(record)
            continue

        same = [alert for alert in found if alert["player"] == record["Bet on"]]
        other = [alert for alert in found if alert["player"] != record["Bet on"]]
        text, title = alert_text(same or other, bool(same))
        out.append({
            **record,
            "_alert": text,
            "_title Alerted": title,
            "_alert_side": "Bet on" if same else "Opponent",
        })

    return out


# Superbet pays 0.88 x stake x odds; alerts keep its price as the app shows it.
SUPERBET_TAX_FACTOR = 0.88


class ProvisionalCLV(float):
    """A CLV worked out before the start (alert_clv), shown with a leading ≈."""


def format_clv(value: float) -> str:
    text = format_ev(value)
    return f"≈{text}" if isinstance(value, ProvisionalCLV) else text


def fair_now(
    player: str, sightings: list[dict], now: datetime
) -> tuple[float | None, str, float | None]:
    """(power-devigged probability of player, Pinnacle price, hours ago) at Pinnacle's
    newest snapshot up to now, else (None, '', None)."""
    import evaluate_ev
    import match_results

    from betting.margin import remove_margin

    tokens = match_results.name_tokens(player)

    for sighting in reversed(sightings):
        seen = evaluate_ev.parse_time(sighting["captured_at"])
        odds = [number(sighting.get("odds1")), number(sighting.get("odds2"))]
        side = evaluate_ev.side_of(sighting["player1"], sighting["player2"], tokens)

        if seen is None or seen > now or not side or not all(o and o > 1 for o in odds):
            continue

        fair = remove_margin(odds, REFERENCE_METHOD)
        return fair[side - 1], f"{odds[side - 1]:.2f}", (now - seen).total_seconds() / 3600

    return None, "", None


def add_alert_clv(
    records: list[dict], alerts: list[dict], sources: dict, now: datetime
) -> list[dict]:
    """Before the start, an alerted row's empty CLV becomes the alert's price (Superbet's
    after tax) x Pinnacle's power-devigged probability now - 1: whether the bet placed
    on the alert is still +EV. Not a closing line yet, hence ProvisionalCLV (≈)."""
    import evaluate_ev

    by_match: dict[tuple, list[dict]] = {}

    for alert in alerts:
        if alert["price"]:
            key = frozenset((alert["player1"], alert["player2"])), alert["tournament"]
            by_match.setdefault(key, []).append(alert)

    out = []

    for record in records:
        found = by_match.get(match_key(record), [])

        if "CLV" not in record or record["CLV"] is not None or not found:
            out.append(record)
            continue

        key = evaluate_ev.match_key(record["Bet on"], record["Opponent"], record["Tournament"])
        sightings = sources.get("sightings", {}).get(key, [])
        start = known_start(record, sightings)

        if start is not None and start <= now:
            out.append(record)
            continue

        same = [alert for alert in found if alert["player"] == record["Bet on"]]
        chosen = sorted(
            same or found,
            key=lambda alert: alert["at"] or datetime.max.replace(tzinfo=timezone.utc),
        )
        values, lines = [], []

        for alert in chosen:
            fair, pinnacle, hours = fair_now(alert["player"], sightings, now)

            if fair is None:
                continue

            tax = SUPERBET_TAX_FACTOR if alert["book"] == "Superbet" else 1.0
            value = alert["price"] * tax * fair - 1
            values.append(value)
            lines.append(
                f"{alert['book']} {alert['price']:.2f}"
                + (" (after tax {:.2f})".format(alert["price"] * tax) if tax != 1 else "")
                + f" on {alert['player']}: {value:+.1%}"
            )

        if not values:
            out.append(record)
            continue

        title = (
            "Not a closing line yet: the alert's price x Pinnacle's fair probability now "
            f"(Pinnacle {pinnacle}, seen {hours:.1f} h ago) - 1, so whether the bet placed "
            "on the alert is still +EV. " + "; ".join(lines) + "."
        )
        out.append({**record, "CLV": ProvisionalCLV(values[0]), "_title CLV": title})

    return out


def load_alerts() -> list[dict]:
    """The Telegram alerts still in notify's memory (3 days); none if it cannot be read."""
    try:
        import notify

        return notify.sent_alerts()
    except Exception:  # noqa: BLE001 - the table must show without it
        return []


def outcomes_cache() -> dict:
    """Held by st.cache_resource, so every tab and rerun shares one."""
    return {}


def records_cache() -> dict:
    """Held by st.cache_resource, so every tab and rerun shares one."""
    return {}


# ------------------------------------------------------------ track record ---


def session_flags(session_id: str, cache: dict) -> dict[str, list[dict]]:
    """Each book's records in a session with an EV of at least 0 (the slider's lowest
    minimum), read again only when the file changed. Only these are kept, because
    every session is held at once. A failed read keeps the last good ones."""
    path = session_csv(session_id)

    try:
        stat = path.stat()
    except OSError:
        return {}

    signature = (stat.st_mtime_ns, stat.st_size)
    held = cache.get(session_id)

    if held and held[0] == signature:
        return held[1]

    rows = read_session_rows(path)

    if rows is None:
        return held[1] if held else {}

    flags = {
        book: [
            record
            for record in table_records(rows, [book])
            if record["Best EV"] is not None and record["Best EV"] >= 0
        ]
        for book in BOOKS
    }
    cache[session_id] = (signature, flags)

    return flags


def first_flags(
    records: list[dict], book: str, min_ev: float, sightings: dict
) -> list[dict]:
    """Each match once, from the first (oldest) record that reached min_ev before the
    match's latest known start, so it could have been bet at that price. Betclic
    prices below its minimum odds are not bets."""
    import evaluate_ev

    seen, first = set(), []

    for record in records:
        key = match_key(record)

        if key in seen or record["Best EV"] < min_ev or not record["_price"]:
            continue

        if book == "Betclic" and record["_price"] < BETCLIC_MIN_ODDS:
            continue

        pair = evaluate_ev.match_key(record["Bet on"], record["Opponent"], record["Tournament"])
        start = known_start(record, sightings.get(pair, []))
        captured = evaluate_ev.parse_time(record["_captured_at"])

        if start and captured and captured >= start:
            continue

        seen.add(key)
        first.append(record)

    return first


def bettable(record: dict, book: str, min_ev: float) -> bool:
    """A flag first_flags would count: EV at least min_ev at a price that can be bet."""
    return (
        record["Best EV"] >= min_ev
        and bool(record["_price"])
        and not (book == "Betclic" and record["_price"] < BETCLIC_MIN_ODDS)
    )


# A signal's lifetime is measured only if the run after its first flag came within
# this: older sessions were hourly, which would show as lifetimes of an hour or more.
MAX_RUN_GAP_MINUTES = 20


def signal_lifetimes(
    session_ids: list[str],
    held: list[dict[str, list[dict]]],
    first: list[dict],
    book: str,
    min_ev: float,
    sightings: dict,
    rechecks: list[dict] | None = None,
) -> list[dict]:
    """How long each first flag stayed bettable, run by run (sessions oldest first,
    held = their session_flags). It ends at the first later run that no longer has the
    same side at min_ev or more (gone, a lower price, or not found there): minutes =
    that run's start - the first flag's capture, so an upper bound at run spacing.
    Still bettable at the match's start, or at the newest run (which may still be
    collecting), it has not ended: ended False, minutes up to its last bettable run.
    next_gone: ended at the very next run; None with no later run before the start.
    Left out when the next run came more than MAX_RUN_GAP_MINUTES after this one.
    rechecks (recheck_signals.py, alerted bets about 2 minutes after the alert) are
    observations between the runs: one where the side is no longer bettable at min_ev
    ends the signal there."""
    import evaluate_ev

    observed: dict[tuple, list[tuple[datetime, bool]]] = {}

    for check in rechecks or []:
        side = check.get("side")
        checked_at = evaluate_ev.parse_time(check.get("checked_at"))

        if check.get("book") != book or side not in (1, 2) or checked_at is None:
            continue

        bet_on, opponent = check[f"player{side}"], check[f"player{3 - side}"]
        key = (match_key({"Bet on": bet_on, "Opponent": opponent, "Tournament": check["tournament"]}), bet_on)
        price, ev = check.get("price"), check.get("ev")
        ok = (
            ev is not None and ev >= min_ev and bool(price)
            and not (book == "Betclic" and price < BETCLIC_MIN_ODDS)
        )
        observed.setdefault(key, []).append((checked_at, ok))

    alive = [
        {
            (match_key(record), record["Bet on"]): record
            for record in flags.get(book, [])
            if bettable(record, book, min_ev)
        }
        for flags in held
    ]
    where: dict[tuple, int] = {}

    for index, records in enumerate(alive):
        for record in records.values():
            where.setdefault((match_key(record), record["_captured_at"]), index)

    lifetimes = []

    for flag in first:
        index = where.get((match_key(flag), flag["_captured_at"]))
        flagged = evaluate_ev.parse_time(flag["_captured_at"])

        if index is None or flagged is None:
            continue

        gap = None
        if index + 1 < len(alive):
            this_run, next_run = session_started(session_ids[index]), session_started(session_ids[index + 1])
            if this_run and next_run:
                gap = (next_run - this_run).total_seconds() / 60
                if gap > MAX_RUN_GAP_MINUTES:
                    continue

        key = (match_key(flag), flag["Bet on"])
        pair = evaluate_ev.match_key(flag["Bet on"], flag["Opponent"], flag["Tournament"])
        start = known_start(flag, sightings.get(pair, []))
        last, ended, checked = flagged, None, 0

        # Later runs and rechecks, in time order.
        events: list[tuple[datetime, int | None, bool]] = []
        for later in range(index + 1, len(alive)):
            run_start = session_started(session_ids[later])
            if run_start is None:
                break
            events.append((run_start, later, key in alive[later]))
        events += [(at, None, ok) for at, ok in observed.get(key, []) if at > flagged]
        events.sort(key=lambda event: event[0])

        if events and events[0][1] is None:
            gap = (events[0][0] - flagged).total_seconds() / 60

        for at, later, present in events:
            if start and at >= start:
                break

            if not present:
                # The newest run may still be collecting: not a sign it is gone.
                if later is None or later < len(alive) - 1:
                    ended = at
                    checked += 1
                break

            checked += 1
            if later is None:
                last = at
            else:
                last = evaluate_ev.parse_time(alive[later][key]["_captured_at"]) or last

        lifetimes.append({
            "minutes": ((ended or last) - flagged).total_seconds() / 60,
            "seen": (last - flagged).total_seconds() / 60,
            "ended": ended is not None,
            "next_gone": None if not checked else ended is not None and checked == 1,
            "gap": gap,
        })

    return lifetimes


# Runs every 5 minutes since 15 Nov 2026: once this many ended signals were measured
# at that spacing, only they count, as the 15-minute ones can never read below 15 min.
FAST_RUN_GAP_MINUTES = 7
MIN_FAST_SIGNALS = 10


def lifetime_stats(lifetimes: list[dict]) -> dict:
    """Median minutes of the signals that ended, the share of them gone within that
    median, and the share of those checked at a later run that were gone by the very
    next one. Measured at 5-minute spacing only, once there are MIN_FAST_SIGNALS."""
    fast = [life for life in lifetimes if life.get("gap") is not None and life["gap"] <= FAST_RUN_GAP_MINUTES]
    if sum(life["ended"] for life in fast) >= MIN_FAST_SIGNALS:
        lifetimes = fast

    # The middle signal itself (the upper one of an even count), not a mean of two:
    # with runs 15 minutes apart that would read "7–22 min", times no signal had.
    finished = sorted((life for life in lifetimes if life["ended"]), key=lambda life: life["minutes"])
    middle = finished[len(finished) // 2] if finished else None
    ended = [life["minutes"] for life in finished]
    checked = [life["next_gone"] for life in lifetimes if life["next_gone"] is not None]
    gaps = [life["gap"] for life in lifetimes if life.get("gap") is not None]
    lasts = middle["minutes"] if middle else None

    return {
        "lasts": lasts,
        "lasts_seen": middle["seen"] if middle else None,
        "lasts_ended": len(ended),
        "lasts_open": len(lifetimes) - len(ended),
        # Within the median as it is shown (minutes_text's rounding).
        "lasts_within": (
            sum(minutes <= shown_minutes(lasts) + 0.05 for minutes in ended) / len(ended) if ended else None
        ),
        # Median time from a flag to the next look at it: a run, or a recheck about
        # 2 minutes after the alert (2.5 min after the Pinnacle snapshot).
        "lasts_spacing": shown_minutes(statistics.median(gaps)) if gaps else None,
        "gone_next": sum(checked) / len(checked) if checked else None,
        "gone_checked": len(checked),
    }


def track_stats(flags: list[dict]) -> dict:
    """One book's record of first flags with Result and CLV (add_outcomes). Profit
    counts completed matches only, stake 1 at the flagged price (Superbet's after
    tax): how a retirement or walkover settles is not checked, as in evaluate_ev."""
    settled = [record for record in flags if record["Result"] in ("Won", "Lost")]
    profits = [
        record["_price"] - 1 if record["Result"] == "Won" else -1.0 for record in settled
    ]
    clvs = [record["CLV"] for record in flags if record["CLV"] is not None]
    # EV = price x fair - 1 at the flag, for either book (a Superbet row without a
    # Betclic match has no probability column).
    fair = [(1 + record["Best EV"]) / record["_price"] for record in settled]

    def mean(values: list[float]) -> float | None:
        return statistics.mean(values) if values else None

    return {
        "signals": len(flags),
        "won": sum(profit > 0 for profit in profits),
        "lost": sum(profit < 0 for profit in profits),
        "pending": sum(record["Result"] == "Pending" for record in flags),
        "no_result": sum(record["Result"] == "No result" for record in flags),
        "other": sum(record["Result"].endswith(")") for record in flags),
        "win_rate": mean([float(profit > 0) for profit in profits]),
        "expected": mean(fair),
        "roi": mean(profits),
        "roi_margin": (
            1.96 * statistics.pstdev(profits) / len(profits) ** 0.5
            if len(profits) > 1
            else None
        ),
        "units": sum(profits),
        "ev": mean([record["Best EV"] for record in flags]),
        "clv": mean(clvs),
        "clv_positive": mean([float(clv > 0) for clv in clvs]),
        "clv_count": len(clvs),
    }


def settled_outcomes(flags: list[dict], sources: dict, now: datetime, memo: dict) -> list[dict]:
    """add_outcomes, keeping each settled match's Result and CLV until the sources
    change: linking a result costs about 50 ms a match on the VM, and every new row
    of a run reruns the page. Pending ones are worked out again each time."""
    signature = sources.get("signature")

    if memo.get("signature") != signature:
        memo.clear()
        memo["signature"] = signature

    def key(flag: dict) -> tuple:
        return match_key(flag), flag["_captured_at"], flag["_price"]

    fresh = {}

    for record in add_outcomes([flag for flag in flags if key(flag) not in memo], sources, now):
        (fresh if record["Result"] == "Pending" else memo)[key(record)] = record

    return [memo.get(key(flag)) or fresh[key(flag)] for flag in flags]


def track_record(
    sessions: list[str],
    cache: dict,
    sources: dict,
    min_ev: float,
    now: datetime,
    books: list[str] = BOOKS,
    rechecks: list[dict] | None = None,
) -> dict[str, dict]:
    """Each book shown with at least one first flag -> track_stats. sessions are
    newest first, as list_sessions gives them."""
    session_ids = list(reversed(sessions))
    oldest_first = [session_flags(session_id, cache) for session_id in session_ids]
    record = {}

    for book in BOOKS:
        if book not in books:
            continue

        records = [flag for flags in oldest_first for flag in flags.get(book, [])]
        flags = first_flags(records, book, min_ev, sources["sightings"])

        if flags:
            memo = cache.setdefault(("outcomes", book), {})
            record[book] = track_stats(settled_outcomes(flags, sources, now, memo))
            record[book].update(lifetime_stats(signal_lifetimes(
                session_ids, oldest_first, flags, book, min_ev, sources["sightings"], rechecks
            )))

    return record


# TrackLoader's session_flags, kept between server starts: reading every session
# took 44 s for 888 sessions on the VM (15 Nov 2026), the file loads in 0.04 s. A
# cache only (each entry is checked against its session file's size and time), so
# backup_data.sh leaves data/cache out.
TRACK_CACHE_FILE = ROOT / "data" / "cache" / "track_flags.pkl"
# TrackLoader's finished records (a few KB), so a restarted server shows the last
# numbers at once instead of a Loading card for the ~40 s cold read; they are
# replaced as soon as the thread has worked out new ones.
TRACK_RESULTS_FILE = ROOT / "data" / "cache" / "track_results.pkl"


def load_flag_cache(path: Path = TRACK_CACHE_FILE) -> dict:
    try:
        with open(path, "rb") as handle:
            held = pickle.load(handle)
    except Exception:  # missing, cut short or from older code: read the sessions again
        return {}

    return held if isinstance(held, dict) else {}


def save_flag_cache(cache: dict, path: Path = TRACK_CACHE_FILE) -> None:
    """The session_flags entries (str keys), written to a temporary file first so a
    reader never sees half of one."""
    held = {key: value for key, value in cache.items() if isinstance(key, str)}
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".tmp")

    with open(partial, "wb") as handle:
        pickle.dump(held, handle, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(partial, path)


def save_results(results: dict, path: Path = TRACK_RESULTS_FILE) -> None:
    """TrackLoader's records by (min EV, books), written like save_flag_cache."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".tmp")

    with open(partial, "wb") as handle:
        pickle.dump(results, handle, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(partial, path)


def flag_signatures(cache: dict) -> dict:
    return {key: value[0] for key, value in cache.items() if isinstance(key, str)}


# load_outcome_sources fills one shared cache, from the page and from TrackLoader's thread.
SOURCES_LOCK = threading.Lock()
# How long a full run waits for the track record before drawing a placeholder. A warm
# one takes about 0.08 s on the VM, so only the first, cold one (about 25 s) shows it.
TRACK_WAIT_SECONDS = 1.0


class TrackLoader:
    """Works out the track record in a thread, so the page never waits for the cold
    read of every session. A full run asks for its minimum EV and books (request);
    the header fragment checks whether it is there yet (ready). One per server."""

    def __init__(
        self,
        sources_cache: dict,
        cache_file: Path | None = TRACK_CACHE_FILE,
        results_file: Path | None = TRACK_RESULTS_FILE,
    ) -> None:
        self.sources_cache = sources_cache
        self.cache_file = cache_file
        self.results_file = results_file
        # session_flags and settled_outcomes, used by the thread only
        self.cache: dict = load_flag_cache(cache_file) if cache_file else {}
        # The last saved records: shown at once, recomputed on the first request.
        self.results: dict[tuple, dict] = load_flag_cache(results_file) if results_file else {}
        self.error: str | None = None
        self.wanted: tuple | None = None
        self.finished: tuple | None = None
        self.thread: threading.Thread | None = None
        self.done = threading.Condition()

    def request(self, min_ev: float, books: list[str], wait: float = 0.0) -> dict | None:
        """Works the record out again (the sessions may have changed) and returns it
        if that takes at most wait, else the last one for this choice, or None."""
        key = (min_ev, tuple(books))

        with self.done:
            self.wanted = key
            self.finished = None

            if self.thread is None:
                self.thread = threading.Thread(target=self.work, daemon=True)
                self.thread.start()

            self.done.wait_for(lambda: self.finished == key, timeout=wait)
            return self.results.get(key)

    def ready(self, min_ev: float, books: list[str]) -> bool:
        with self.done:
            return (min_ev, tuple(books)) in self.results or self.error is not None

    def work(self) -> None:
        while True:
            with self.done:
                key, self.wanted = self.wanted, None

                if key is None:
                    self.thread = None
                    return

            try:
                with SOURCES_LOCK:
                    sources = load_outcome_sources(self.sources_cache)

                held = flag_signatures(self.cache)
                import recheck_signals

                record = track_record(
                    list_sessions(), self.cache, sources, key[0],
                    datetime.now(timezone.utc), list(key[1]),
                    rechecks=recheck_signals.load_rechecks(),
                )
                error = None

                if self.cache_file and flag_signatures(self.cache) != held:
                    try:
                        save_flag_cache(self.cache, self.cache_file)
                    except OSError:
                        pass  # only the next server start gets slower
            except Exception as exc:  # shown on the card; the page goes on
                record, error = None, f"{type(exc).__name__}: {exc}"

            with self.done:
                changed = record is not None and self.results.get(key) != record
                if record is not None:
                    self.results[key] = record
                self.error = error
                self.finished = key
                self.done.notify_all()
                saved = dict(self.results)

            if changed and self.results_file:
                try:
                    save_results(saved, self.results_file)
                except Exception:
                    pass  # only the next server start shows Loading again


def track_loader() -> TrackLoader:
    """Held by st.cache_resource, so every tab and rerun shares one."""
    import streamlit as st

    return TrackLoader(st.cache_resource(show_spinner=False)(outcomes_cache)())


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


def superbet_collected(info: dict) -> bool:
    """Whether this session has a Superbet CSV: rows get Superbet prices only once
    Betclic has queued them, so before that the rows alone cannot tell."""
    session_id = info.get("session_id")
    return bool(session_id) and (SUPERBET_RAW_DIR / f"superbet_{session_id}.csv").exists()


def tennis_requests_today(session_id: str) -> int | None:
    """Pinnacle tennis requests on this run's day (UTC on the VM) up to and including
    this run, counted from the snapshots: each one is one successful request."""
    match = re.match(r"session_(\d{8})_", session_id or "")

    if not match:
        return None

    own = f"pinnacle_{session_id}.csv"
    return sum(
        path.name <= own for path in PINNACLE_RAW_DIR.glob(f"pinnacle_session_{match[1]}_*.csv")
    )


def pinnacle_source(info: dict) -> dict | None:
    """Where the run's Pinnacle odds came from and the PinnFeed quota after its
    request (user, 20 Nov 2026), for the tip on the Pinnacle box. None for runs
    before PinnFeed or without a status. A finished run keeps the block under
    "performance", a running one at the top level."""
    pinnacle = pinnacle_info(info)

    if pinnacle.get("source") != "pinnfeed":
        return None

    def number(key: str) -> int | None:
        try:
            return int(pinnacle.get(key))
        except (TypeError, ValueError):
            return None

    limit = number("quota_limit") or PINNFEED_DAILY_LIMIT
    remaining = number("quota_remaining")

    return {
        "tennis_today": tennis_requests_today(info.get("session_id", "")),
        "used": limit - remaining if remaining is not None else None,
        "limit": limit,
        "delay": number("delay_seconds") or PINNFEED_DELAY_SECONDS,
    }


def pinnacle_tip(source: dict) -> str:
    """Short lines, one fact each (user, 20 Nov 2026); the name links to PinnFeed."""
    lines = [f'Source: <a href="{PINNFEED_SITE}">PinnFeed</a>']

    if source.get("tennis_today") is not None:
        lines.append(html.escape(
            f"Today: {source['tennis_today']} / {TENNIS_RUNS_PER_DAY} ({source['limit']:,} free)"
        ))
    lines.append(html.escape(f"Delay: {source['delay']} s"))
    return "<br>".join(lines)


# Session ID -> name pairs its Pinnacle snapshot listed as already started. A snapshot
# is written once, so a found one is never read again.
STARTED_PAIRS: dict[str, list[frozenset]] = {}


def started_pairs(session_id: str) -> list[frozenset]:
    """Matches Pinnacle listed as started in this run's snapshot (PinnFeed's
    status "started", since 20 Nov 2026). The collectors skip them, so they never
    get a row."""
    if session_id in STARTED_PAIRS:
        return STARTED_PAIRS[session_id]

    path = PINNACLE_RAW_DIR / f"pinnacle_{session_id}.csv"
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            pairs = [
                frozenset((row.get("player1", ""), row.get("player2", "")))
                for row in csv.DictReader(handle)
                if str(row.get("status", "")).lower() == "live"
            ]
    except OSError:
        return []

    STARTED_PAIRS[session_id] = pairs
    return pairs


def started_without_row(session_id: str, rows: list[dict]) -> int:
    """Started matches with no row in the session: counted apart from Waiting, which
    would otherwise never empty for a finished run (user, 20 Nov 2026)."""
    if not session_id:
        return 0

    seen = {
        frozenset((row.get("pinnacle_player1", ""), row.get("pinnacle_player2", "")))
        for row in rows
    }
    return sum(pair not in seen for pair in started_pairs(session_id))


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
    # Started matches never reach the books, so every "x / total" leaves them out.
    started = started_without_row(info.get("session_id", ""), rows) if expected else 0
    expected = expected - started if expected else expected

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
    if superbet_found is None and superbet_collected(info):
        # Superbet is read before Betclic, so its bar shows from the start of a run.
        superbet_found = "0"

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
        "started": started,
        "betclic_gap": capture_gap(latest, "betclic_captured_at"),
        "superbet_gap": capture_gap(latest, "superbet_captured_at"),
        "pinnacle_source": pinnacle_source(info),
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
    "CLV": format_clv,
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

    if column == "Alerted" and value:
        return DIMMED if str(value).startswith(ALERT_OTHER_SIDE) else EV_AMBER

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

    def classes(column: str, record: dict | None = None) -> str:
        names = ["number"] if column in numeric else []

        if record is not None and record.get("_alert_side") == column:
            names += ["alerted" if column == "Bet on" else "alerted-other"]

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
        title = value if full_name else record.get(f"_title {column}")
        bell = ""

        if record.get("_alert_side") == column:
            bell = f'<span class="bell">{ALERT_BELL}</span>'
            alert_line = f"{record['_alert']}. {record['_title Alerted']}"
            title = f"{value}\n{alert_line}" if full_name else alert_line

        return (
            f"<td{classes(column, record)}"
            f"{attribute('style', cell_css(column, value, dimmed, min_ev))}"
            f"{attribute('title', title)}"
            f"{attribute('data-flip', column in TAX_SWITCH_CLASSES and not blank)}>"
            f"{bell}{html.escape(str(text))}</td>"
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


def cards_html(
    records: list[dict], min_ev: float, superbet: bool, betclic: bool = True
) -> str:
    """The same records as table_html, one card each, for phone-width screens (CSS
    shows one or the other). Best EV leads, then one tile per book for the Bet on
    side; doubles names stay whole, since a card can wrap them."""

    def attribute(name: str, value: Any) -> str:
        return f' {name}="{html.escape(str(value))}"' if value else ""

    def text(column: str, record: dict) -> str:
        value = record.get(column)

        return "--" if value is None or value == "" else CELL_FORMATS.get(column, str)(value)

    def ev(column: str, record: dict) -> str:
        return (
            f'<span class="ev"{attribute("style", ev_css(record.get(column), min_ev))}>'
            f"EV {html.escape(text(column, record))}</span>"
        )

    def tile(label: str, price: str, *subs: str) -> str:
        lines = "".join(f'<div class="sub">{sub}</div>' for sub in subs if sub)

        return (
            f'<div class="mc-book"><div class="label">{label}</div>'
            f'<div class="price">{html.escape(price)}</div>{lines}</div>'
        )

    cards = []

    for record in records:
        dimmed = record.get("EV Betclic") is None and record.get("EV Superbet") is None
        best = record.get("Best EV")
        best_style = ev_css(best, min_ev)
        meta = " · ".join(
            html.escape(part)
            for part in (
                text("Starts", record) if record.get("Starts") else "",
                record.get("Tournament") or "",
            )
            if part
        )
        tiles = []

        if betclic:
            tiles.append(tile(
                "Betclic",
                text("Betclic", record),
                ev("EV Betclic", record),
                f"margin {html.escape(text('Margin Betclic', record))}",
            ))
        if superbet:
            tiles.append(tile(
                "Superbet",
                text("Superbet", record),
                ev("EV Superbet", record),
                f"net {html.escape(text('Superbet net', record))}",
            ))
        tiles.append(tile(
            "Pinnacle",
            text("Pinnacle", record),
            f"fair {html.escape(text('Fair prob', record))}",
            f"margin {html.escape(text('Margin Pinnacle', record))}",
        ))

        extras = []

        for column in ("Alerted", "Result", "CLV", "Session"):
            source = "_alert" if column == "Alerted" else column

            if source not in record:
                continue

            style = cell_css(column, record[source], False, min_ev)
            extras.append(
                f'<span{attribute("title", record.get(f"_title {column}"))}>'
                f'<span class="label">{column}</span>'
                f'<span{attribute("style", style)}>'
                f"{html.escape(text(source, record))}</span></span>"
            )

        cards.append(
            f'<div class="match-card{" dimmed" if dimmed else ""}">'
            '<div class="mc-head"><div class="mc-names">'
            f'<div class="mc-bet" style="{BET_ON}">{html.escape(record["Bet on"])}</div>'
            f'<div class="mc-vs">vs {html.escape(record["Opponent"])}</div></div>'
            f'<div class="mc-best"{attribute("style", best_style)}>'
            f'{html.escape(format_ev(best) if best is not None else "no EV")}</div></div>'
            f'<div class="mc-meta">{meta}</div>'
            f'<div class="mc-books">{"".join(tiles)}</div>'
            + (f'<div class="mc-extra">{"".join(extras)}</div>' if extras else "")
            + "</div>"
        )

    return f'<div class="match-cards">{"".join(cards)}</div>'


def share(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else "--"


def overview_counts(value: str) -> tuple[int, int | None]:
    """'99 / 110' -> (99, 110) and '99' -> (99, None)."""
    part, _, whole = value.partition(" / ")

    return int(part), int(whole) if whole else None


def overview_meters(stats: dict, total: int, books: list[str] = BOOKS) -> str:
    rows = [
        ("Pinnacle", "Pinnacle matches processed"),
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


# The EV status donuts (user, 15 Nov 2026: two side by side fit a phone's width where
# the two split-bar cards took a full row each). CSS only: st.html's sanitizer removes
# SVG, so the ring is a conic-gradient with its middle masked out (.donut-ring).
DONUT_MIN_SHARE = 0.04  # one match of 41 still shows as a visible segment


def donut_lengths(counts: list[int], circumference: float) -> list[float]:
    """Each count's arc, at least DONUT_MIN_SHARE of the ring; the larger parts give
    up what the small ones gain, in proportion."""
    whole = sum(counts)
    floor = DONUT_MIN_SHARE * circumference
    raw = [circumference * count / whole for count in counts]
    small = [length < floor for length in raw]
    lifted = floor * sum(small)
    rest = sum(length for length, is_small in zip(raw, small) if not is_small)
    scale = (circumference - lifted) / rest if rest else 0

    return [floor if is_small else length * scale for length, is_small in zip(raw, small)]


def donut_gradient(drawn: list[tuple[str, int, str, str]]) -> str:
    """conic-gradient stops: the segments edge to edge (user, 15 Nov 2026: no gaps)."""
    if not drawn:
        return OVERVIEW_WAITING

    stops, start = [], 0.0

    for (_, _, color, _), length in zip(drawn, donut_lengths([part[1] for part in drawn], 100.0)):
        end = start + length
        stops.append(f"{color} {start:.2f}% {end:.2f}%")
        start = end

    return f"conic-gradient({', '.join(stops)})"


def overview_donut(
    book: str,
    calculated: int,
    no_ev: int,
    reasons: str,
    matches: int,
    total: int,
    started: int = 0,
) -> str:
    """Where every Pinnacle match ended up at one book, as a ring with the matches that
    got an EV in its centre and the counts beside it. VERIFY, AMBIGUOUS and ERROR count
    as not matched, because EV is calculated for FOUND only. total leaves out the
    started matches, which the books were never asked about."""
    parts = [
        ("EV calculated", calculated, OVERVIEW_BLUE, ""),
        ("No EV", no_ev, OVERVIEW_NO_EV, reasons),
        ("Not matched", max(matches - calculated - no_ev, 0), OVERVIEW_NOT_MATCHED, ""),
        ("Started", started, OVERVIEW_STARTED, "skipped"),
        ("Waiting", max(total - matches, 0), OVERVIEW_WAITING, ""),
    ]
    # A part at zero draws nothing. Waiting only means something during a run.
    drawn = [part for part in parts if part[1]]
    whole = sum(count for _, count, _, _ in drawn)
    title = ", ".join(
        f"{label} {count} of {whole} ({share(count, whole)})" for label, count, _, _ in drawn
    )
    legend = "".join(
        f'<div><span class="overview-swatch" style="background-color: {color}"></span>'
        f"{label} <b>{count}</b>"
        + (f' <span class="overview-note">({html.escape(note)})</span>' if note else "")
        + "</div>"
        for label, count, color, note in parts
        # Only what the ring shows (user, 15 Nov 2026), and always the EV count.
        if count or label == "EV calculated"
    )

    return (
        '<div class="donut-block">'
        f'<div class="donut-book">{html.escape(book)}</div>'
        '<div class="donut-body">'
        f'<div class="donut" role="img" aria-label="{html.escape(book)}: {html.escape(title)}" '
        f'title="{html.escape(title)}">'
        f'<div class="donut-ring" style="background: {donut_gradient(drawn)}"></div>'
        f'<div class="donut-value">{calculated}<span class="donut-whole">/{total}</span></div>'
        "</div>"
        f'<div class="donut-legend">{legend}</div>'
        "</div></div>"
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


def signal_tiles(stats: dict, books: list[str] = BOOKS) -> list[str]:
    """Colours follow the table: amber above 0, green from the threshold up. A book
    whose counts are both 0 is left out (user, 15 Nov 2026), so no signal = []."""

    def hit(value: str, css: str) -> str:
        return css.split(";")[0] if int(value) else ""

    threshold = next(label for label in stats if label.startswith("EV(ref) >= "))
    limit = threshold.removeprefix("EV(ref) >= ")
    tiles = []

    if "Betclic" in books and (int(stats["EV(ref) > 0%"]) or int(stats[threshold])):
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

    superbet_threshold = stats.get(f"Superbet {threshold}", "0")
    if (
        "Superbet" in books
        and "Superbet EV(ref) > 0%" in stats
        and (int(stats["Superbet EV(ref) > 0%"]) or int(superbet_threshold))
    ):
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

    # Every count 0: one greyed "None" instead of a row of zeros. Its explanation is a
    # tooltip shown on hover, or on a tap on phones (tabindex makes it focusable).
    return tiles


def overview_positive(stats: dict, books: list[str] = BOOKS) -> str:
    """The signal tiles, or with none a greyed "None" over a "Signals" label (the card
    then has no heading of its own; user, 15 Nov 2026)."""
    tiles = signal_tiles(stats, books)

    if not tiles:
        tip = f"No match with EV ({REFERENCE_METHOD} method) > 0%"
        tiles = [tip_tile("Signals", "None", tip, "overview-empty")]

    return f'<div class="overview-tiles">{"".join(tiles)}</div>'


def signed_css(value: float | None) -> str:
    """Green above 0, red below, like Result."""
    if not value:
        return ""

    return EV_GREEN.split(";")[0] if value > 0 else EV_RED


def lifetime_text(stats: dict) -> str:
    """Roughly how long a signal stays: the median signal was last seen at one run and
    gone at a later one, so it lasted between the two. Gone by the very next run reads
    "< 15 min" (or "< 5 min" with runs 5 minutes apart)."""
    upper, lower = stats.get("lasts"), stats.get("lasts_seen")

    if upper is None:
        return "--"

    if lower is None or lower < 1:
        return f"< {minutes_text(upper)}"

    return f"{minutes_text(lower).removesuffix(' min')}–{minutes_text(upper)}"


def lifetime_help(stats: dict) -> str:
    """The Lasts tile's tip, shown on hover or a tap: how many were gone within the
    shown time, and how long after the flag they were looked at again (the next run,
    or a recheck about 2 minutes after the alert; user, 20 Nov 2026: "after", not
    "every")."""
    within, lasts = stats.get("lasts_within"), stats.get("lasts")

    if within is None or lasts is None:
        return "No signal has ended yet"

    spacing = stats.get("lasts_spacing")
    checked = f", checked again {minutes_text(spacing)} after the odds were read" if spacing else ""
    open_text = (
        f" {stats['lasts_open']} still bettable at the start are left out."
        if stats.get("lasts_open")
        else ""
    )

    return (
        f"{within:.0%} of {stats['lasts_ended']} signals were gone within "
        f"{minutes_text(lasts)}{checked}.{open_text}"
    )


def tip_tile(label: str, value: str, tip: str, value_class: str = "", end: bool = False) -> str:
    """A tile whose explanation shows on hover, or on a tap on phones (tabindex makes
    it focusable; a tap elsewhere hides it). end: the tile is in the right column on
    phones, so its tip opens leftwards."""
    css = f"value {value_class}".strip()

    return (
        f'<div class="overview-tile has-tip{" end" if end else ""}" tabindex="0">'
        f'<div class="{css}">{html.escape(value)}</div>'
        f'<div class="label">{html.escape(label)}</div>'
        f'<div class="tip">{html.escape(tip)}</div></div>'
    )


def shown_minutes(minutes: float) -> float:
    """Minutes as minutes_text shows them: tenths under 10 min, so a recheck 2.5 min
    after the snapshot does not read as 3."""
    return round(minutes, 1) if minutes < 9.95 else float(round(minutes))


def minutes_text(minutes: float | None) -> str:
    if minutes is None:
        return "--"

    if minutes >= 90:
        return f"{minutes / 60:.1f} h"

    return f"{shown_minutes(minutes):g} min"


def overview_track(record: dict[str, dict], min_ev: float = 0.02) -> str:
    """One row of tiles per book. CLV first: it settles in a day, while ROI needs
    hundreds of bets before its range stops covering 0. The rules (first flag at
    min_ev, Betclic's minimum odds) are in the Signals tip and a note under Betclic
    (user, 15 Nov 2026), not in a heading."""

    def pct(value: float | None, digits: int = 1, signed: bool = True) -> str:
        if value is None:
            return "--"

        return f"{value:+.{digits}%}" if signed else f"{value:.0%}"

    rows = []

    for book, stats in record.items():
        expected = stats["expected"]
        margin = stats["roi_margin"]

        tiles = [
            tip_tile(
                "Signals",
                str(stats["signals"]),
                f"First flag at EV ≥ {min_ev:.1%}",
            ),
            overview_tile(
                "Avg CLV",
                pct(stats["clv"]),
                f"Mean CLV of the {stats['clv_count']} matches that have started: the "
                "flagged price against Pinnacle's last power-devigged price before the "
                "start. Above 0 means the price beat the close.",
                signed_css(stats["clv"]),
            ),
            overview_tile(
                "CLV > 0",
                pct(stats["clv_positive"], signed=False),
                f"Share of the {stats['clv_count']} started matches whose price beat the close",
            ),
            overview_tile(
                "Won-Lost",
                f"{stats['won']}-{stats['lost']}",
                "Completed matches only",
            ),
            overview_tile(
                "Win rate"
                + (f" (exp. {expected:.0%})" if expected is not None else ""),
                pct(stats["win_rate"], signed=False),
                "Won share of completed matches; expected = mean of Pinnacle's "
                "power-devigged probability for the side bet",
            ),
            overview_tile(
                "ROI" + (f" ±{margin:.0%}" if margin is not None else ""),
                pct(stats["roi"]),
                "Profit per stake of 1 on completed matches, with its 95% range. "
                "While the range covers 0 the results prove nothing either way.",
                signed_css(stats["roi"]),
            ),
            overview_tile(
                "Units",
                f"{stats['units']:+.2f}",
                "Total profit at a stake of 1 per completed match",
                signed_css(stats["units"]),
            ),
            overview_tile(
                "Avg EV",
                pct(stats["ev"]),
                "Mean EV at the first flag, what the prices promised",
            ),
            tip_tile("Lasts", lifetime_text(stats), lifetime_help(stats), end=True),
        ]
        rows.append(
            f'<div class="track-book">{html.escape(book)}</div>'
            f'<div class="overview-tiles">{"".join(tiles)}</div>'
        )
        if book == "Betclic":
            # Under the tiles: an empty first column on desktop, its own line on phones.
            rows.append(
                '<div></div>'
                f'<div class="track-note">Odds ≥ {BETCLIC_MIN_ODDS:.2f}</div>'
            )

    return f'<div class="track-rows">{"".join(rows)}</div>'


def track_card(
    record: dict[str, dict] | None,
    min_ev: float,
    books: list[str] = BOOKS,
    error: str | None = None,
) -> str:
    """The Track record card, for overview_html's last row, under its "Statistics
    (all runs)" title and with no heading of its own. None = still loading."""
    if record:
        body = overview_track(record, min_ev)
    else:
        if record is not None:
            text = "No match has reached this EV before its start yet."
        elif error:
            text = f"Could not work out the track record: {error}"
        else:
            text = "Loading (under a minute)…"
        loading = record is None and not error
        body = (
            '<div class="overview-empty overview-loading"><span class="spinner"></span>'
            f"<span>{html.escape(text)}</span></div>"
            if loading
            else f'<div class="overview-empty">{html.escape(text)}</div>'
        )

    return f'<div class="overview-card">{body}</div>'


def pinnacle_age_text(age: float) -> str:
    """Seconds stop mattering once the prices are stale. TABLE_SCRIPT's ageText keeps
    this format going between server ticks."""
    seconds = max(0, round(age))

    if STALE_MINUTES * 60 < seconds < 3600:
        return f"{seconds // 60} min"

    return fmt_age(seconds)


def gap_text(seconds: float) -> str:
    return f"{seconds:.1f} s" if seconds < 60 else fmt_gap(seconds)


def overview_flow(summary: dict, books: list[str] = BOOKS) -> str:
    """How the run's prices arrived (user, 15 Nov 2026): Pinnacle with the age of its
    prices, which TABLE_SCRIPT counts on each second, then each book in the order its
    prices came, the arrow giving the time since the box before it. Each gap is the
    median per match from the Pinnacle capture; a book without data comes last."""
    gaps = [(summary.get(f"{book.lower()}_gap"), book) for book in BOOKS if book in books]
    return flow_html(summary, gaps)


def flow_html(summary: dict, gaps: list[tuple[float | None, str]]) -> str:
    """overview_flow's diagram for any sources after Pinnacle, one after the other:
    (median seconds from the Pinnacle capture or None, name)."""
    return f'<div class="flow">{pinnacle_flow_node(summary)}{"".join(flow_steps(gaps))}</div>'


def fork_html(summary: dict, branches: list[list[tuple[float | None, str]]]) -> str:
    """Pinnacle, then sources read at the same time as branches beside it (the
    Prediction markets tab, user 22 Nov 2026): one row per branch, its first arrow
    the time from Pinnacle, the rest the time from the box before. Branches in the
    order their first prices came."""
    def first(branch: list) -> tuple:
        seconds = branch[0][0] if branch else None
        return (seconds is None, seconds or 0)

    rows = "".join(
        f'<div class="flow-branch">{"".join(flow_steps(branch))}</div>'
        for branch in sorted(branches, key=first)
    )
    return (
        f'<div class="flow flow-fork">{pinnacle_flow_node(summary)}'
        f'<div class="flow-branches">{rows}</div></div>'
    )


def pinnacle_flow_node(summary: dict) -> str:
    """The flow's first box: Pinnacle with the age of its prices, which TABLE_SCRIPT
    counts on each second."""
    age = summary.get("pinnacle_age")
    age_html = "--"

    if age is not None:
        freshness = "stale" if age > STALE_MINUTES * 60 else "fresh"
        age_html = (
            f'<span class="flow-age {freshness}">'
            f'<span data-since="{summary["pinnacle_captured"]}"'
            f' data-stale-after="{STALE_MINUTES * 60:g}">{pinnacle_age_text(age)}</span> ago</span>'
        )

    # The tip names the source, its quota and its delay (user, 20 Nov 2026).
    source = summary.get("pinnacle_source")

    if source:
        return (
            '<div class="flow-node has-tip" tabindex="0">'
            f'<div class="flow-name label">Pinnacle</div><div class="flow-sub">{age_html}</div>'
            f'<div class="tip">{pinnacle_tip(source)}</div></div>'
        )
    return (
        '<div class="flow-node">'
        f'<div class="flow-name">Pinnacle</div><div class="flow-sub">{age_html}</div></div>'
    )


def flow_steps(gaps: list[tuple[float | None, str]]) -> list[str]:
    """An arrow and a box per source in the order the prices came, each arrow the
    time since the box before it; a source without data comes last."""
    gaps = sorted(gaps, key=lambda pair: (pair[0] is None, pair[0] or 0))
    before = 0.0
    steps = []

    for seconds, book in gaps:
        if seconds is None:
            label, title = "--", f"No {book} prices in this run"
        else:
            label = f"+{gap_text(max(seconds - before, 0))}"
            title = f"Median time from the Pinnacle capture to the {book} capture, per match: {gap_text(seconds)}"
            before = seconds
        steps.append(
            # Arrow and box in one step, so a wrapping flow keeps them together.
            '<div class="flow-step">'
            f'<div class="flow-arrow" title="{html.escape(title)}">'
            f'<span class="flow-gap">{label}</span><span class="flow-line"></span></div>'
            f'<div class="flow-node"><div class="flow-name">{html.escape(book)}</div></div></div>'
        )

    return steps


def stale_fact(summary: dict) -> str:
    """Whether the Pinnacle odds are past STALE_MINUTES, under the heading beside the
    run time (user, 15 Nov 2026): green No, red Yes, its limit in a tip shown on hover
    or a tap. TABLE_SCRIPT flips it between server ticks, like the Data flow age."""
    age = summary.get("pinnacle_age")
    stale = age is not None and age > STALE_MINUTES * 60
    value = "--" if age is None else "Yes" if stale else "No"
    css = "" if age is None else (" stale" if stale else " fresh")
    flag = "" if age is None else ' data-stale-flag=""'

    return (
        '<div class="session-stale has-tip" tabindex="0">'
        f'<div class="value{css}"{flag}>{value}</div>'
        '<div class="label">Stale data</div>'
        f'<div class="tip">Pinnacle odds over {STALE_MINUTES:g} min old</div></div>'
    )


def overview_checks(summary: dict, stats: dict, books: list[str] = BOOKS) -> str:
    """The collection flow (overview_flow), then Errors in amber when there are any
    (Stale data is in the session bar)."""
    flow = overview_flow(summary, books)

    # Only in the stats when it matters: see session_summary. They are Betclic's.
    if "Errors" in stats and "Betclic" in books:
        errors = stats["Errors"]
        tile = overview_tile(
            "Errors", errors, "Matches that could not be checked", EV_AMBER if int(errors) else ""
        )
        flow += f'<div class="overview-tiles">{tile}</div>'

    return flow


def failed_sources(session_id: str) -> list[str]:
    """The books that failed in this run, from the run ledger (run_once writes the
    record a few seconds after the run's status says finished)."""
    import run_ledger

    found = run_ledger.find(session_id) or {}
    sources = found.get("sources") or {}
    return [
        label for key, label in PILL_SOURCES.items()
        if (sources.get(key) or {}).get("state") == "failed"
    ]


def session_bar_html(summary: dict, session_id: str) -> str:
    """The overview's heading (user, 15 Nov 2026): "Last run overview" with the run's
    status on its right, then when it ran and whether its Pinnacle prices are stale,
    each a value over a small grey label. Their age is in the Data flow card."""
    status = summary["status"]
    failed = summary.get("failed_sources") or []
    tip = ""

    if status == "finished" and failed:
        status = "failed"
        tip = f"{' and '.join(failed)} failed in this run"
    elif status == "failed":
        tip = "The run failed"

    colour = STATUS_COLOURS.get(status, OVERVIEW_BLUE)
    label = STATUS_LABELS.get(status, status.capitalize())
    title = f' title="{html.escape(tip)}"' if tip else ""
    started = session_started(session_id)
    # Phones hide the year.
    run_on = (
        html.escape(session_id)
        if started is None
        else f'<span class="year">{started:%Y-}</span>{started:%m-%d %H:%M}'
    )
    return (
        '<div class="session-head"><div class="session-title">'
        '<span class="overview-title">Last run overview</span>'
        f'<span class="session-status" style="--status: {colour}"{title}>'
        f"{html.escape(label)}</span></div>"
        '<div class="session-facts">'
        f'<div class="session-fact"><div class="value">{run_on}</div>'
        '<div class="label">Last run on</div></div>'
        f"{stale_fact(summary)}</div></div>"
    )


def track_section(track: str) -> str:
    """The Track record card under its own title: it counts every run, not this one."""
    if not track:
        return ""

    return (
        '<div class="overview-title">Statistics (all runs)</div>'
        f'<div class="overview-cards">{track}</div>'
    )


def overview_html(
    summary: dict,
    ev_only: bool = False,
    ev_stats: dict | None = None,
    books: list[str] = BOOKS,
    track: str = "",
    titled: bool = True,
) -> str:
    """titled=False when session_bar_html is the heading above it. The stats of
    session_summary as two rows of cards, then track (track_card,
    over all sessions) on its own row when given. Coverage stays as bars, since its
    counts are often close together (99 and 105 of 110), which slices do not show; the
    EV status split is one donut per book (overview_donut), with its counts beside it,
    because the two fit one row on a phone. ev_only
    (the EV filter is on) keeps only the Signals card, as narrow as its tiles,
    counted over ev_stats (all sessions) when given."""
    stats = dict(summary["stats"])
    matches, total = summary["matches"], summary["total"]
    started = summary.get("started", 0)
    title = '<div class="overview-title">Last run overview</div>' if titled else ""
    betclic_reasons = ", ".join(
        f"{label.strip()} {value}"
        for label, value in stats.items()
        if label.startswith(SUBGROUP_INDENT)
    )

    def card(heading: str, body: str, size: str = "") -> str:
        title = f'<div class="overview-heading">{html.escape(heading)}</div>' if heading else ""
        return f'<div class="overview-card {size}">{title}{body}</div>'

    if ev_only:
        where = ev_stats is not None
        positive = card(
            f"Signals{' (all sessions)' if where else ''}",
            overview_positive(stats if ev_stats is None else ev_stats, books),
            "fit",
        )

        return (
            '<section class="overview">' + title +
            f'<div class="overview-cards">{positive}</div>'
            + track_section(track)
            + "</section>"
        )

    coverage = [
        card("Matches covered", overview_meters(stats, total, books), "wide")
    ]

    donuts = []

    if "Betclic" in books:
        donuts.append(
            overview_donut(
                "Betclic",
                overview_counts(stats["EV calculated"])[0],
                int(stats["FOUND but no EV"]),
                betclic_reasons,
                matches,
                total,
                started,
            )
        )

    if "Superbet" in books and "Found on Superbet" in stats:
        donuts.append(
            overview_donut(
                "Superbet",
                overview_counts(stats["Found on Superbet"])[0],
                int(stats[SUPERBET_NO_EV_LABEL]),
                "",
                matches,
                total,
                started,
            )
        )

    if donuts:
        coverage.append(
            card("EV status", f'<div class="donut-pair">{"".join(donuts)}</div>', "wide")
        )

    # Data flow left, under the coverage bars, and Signals right (user, 20 Nov 2026).
    results = [
        card("Data flow and age", overview_checks(summary, stats, books), "wide"),
        # "Signals" as the heading over the tiles, or as None's own label.
        card("Signals" if signal_tiles(stats, books) else "", overview_positive(stats, books), "wide"),
    ]

    return (
        '<section class="overview">' + title +
        f'<div class="overview-cards">{"".join(coverage)}</div>'
        f'<div class="overview-cards">{"".join(results)}</div>'
        + track_section(track)
        + "</section>"
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
    "started; hover a value for how close to the start that price was. Before the "
    "start, an alerted match shows ≈: the alert's price x Pinnacle's fair probability "
    "now - 1, i.e. whether that bet is still +EV.",
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
    track: str = "",
) -> None:
    """Runs every second, so it stays cheap: track is made by the full run.
    rows belong to table_session."""
    info = session_info(session_id)
    now = datetime.now(timezone.utc)
    waiting = table_session != session_id
    summary = session_summary([] if waiting else rows, info, min_ev, now)
    summary["failed_sources"] = failed_sources(session_id) if summary["status"] == "finished" else []

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

    st.html(overview_html(summary, ev_only, ev_stats, books, track, titled=False))

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

    alerts = load_alerts()
    shown = add_alerts(shown, alerts)

    if outcomes is not None and filters["show_clv"]:
        shown = add_alert_clv(shown, alerts, outcomes, datetime.now(timezone.utc))

    betclic = "Betclic" in books
    box.html(
        table_html(shown, min_ev, superbet, betclic)
        + cards_html(shown, min_ev, superbet, betclic)
    )


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
    min-height: 2.7rem;
    min-width: 2.4rem;
    padding: 0;
}
.st-key-session_calendar button { position: relative; }
.st-key-session_calendar button::after {
    position: absolute;
    top: 3px;
    right: 4px;
    font-size: 0.72rem;
    line-height: 1;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    color: #e3b341;
}
.st-key-session_calendar [data-testid="stBaseButton-primary"]::after {
    color: rgba(255, 255, 255, 0.85);
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


def day_count_style(days: dict[date, list[str]], month: date) -> str:
    """Each day's session count in its button's top-right corner (PICKER_STYLE places
    and colours it). A button cannot hold HTML, so CSS adds it through the class
    Streamlit gives each keyed widget, st-key-<key>."""
    rules = "".join(
        f'.st-key-day_{day.isoformat()} button::after {{ content: "{len(runs)}"; }}\n'
        for day, runs in sorted(days.items())
        if runs and (day.year, day.month) == (month.year, month.month)
    )

    return f"<style>\n{rules}</style>" if rules else ""


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
            counts = day_count_style(days, month)
            if counts:
                st.html(counts)

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
# Betting EV

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
  Before the start, an alerted match shows **≈** and the alert's price against
  Pinnacle's fair price now: whether the bet placed on the alert is still +EV.
* **🔔 on a name**: a Telegram alert was already sent on that player; hover the name
  for the first alert's book, price and time (on phones, the card's Alerted line). A
  shaded Bet on name is the bet already placed, so a later, smaller EV there is not a
  new bet; a grey bell on the Opponent means the alert was on the other player.
* **Last run overview**: the run's status beside the heading; under it when the run
  started and whether its Pinnacle prices are stale (Stale data: Yes once they are over
  15 minutes old).
* **Data flow and age**: the order the prices were collected in. The Pinnacle box shows how
  long ago its prices were captured (green, red once over 15 minutes); each arrow, how
  much later the next bookmaker's prices were read.
* **Signals**: how many matches reached EV above 0 and the minimum EV at each
  bookmaker (None when no match did).
* **Track record**: every match that reached the minimum EV before its start, counted
  once at its first flag and price (Betclic only from odds of 1.14, the lowest it takes
  for a single). Its average CLV and share above 0 say first whether the edge is real;
  win rate, ROI and units need many more bets, and while ROI's 95% range covers 0 the
  results show nothing either way.
* **Prediction markets** (a subtab of Tennis; Sharp vs soft, Pinnacle against the bookmakers, is the page described above): Polymarket and Kalshi prices next to Pinnacle's for the
  newest run, for comparison only (no alerts, not in the Track record). A market's
  probability is the middle of its bid and ask; a market whose spread is wider than
  the sidebar's limit is greyed out and its EV not trusted. **Agree** marks a side
  that reaches the minimum EV against Pinnacle and every tight market.
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


# The sport tabs: larger than Streamlit's page links, with a line under the row.
SPORT_TABS_STYLE = """<style>
.st-key-sport_tabs {
    padding-bottom: 0.6rem;
    margin-bottom: 0.4rem;
    border-bottom: 1px solid rgba(128, 128, 128, 0.25);
}
.st-key-sport_tabs a {
    padding: 0.45rem 1.1rem !important;
}
.st-key-sport_tabs a p,
.st-key-sport_tabs a span {
    font-size: 1.15rem !important;
    font-weight: 600;
}
/* The same shade Streamlit gives the page shown. */
.st-key-sport_tab_parent a { background-color: rgba(172, 177, 195, 0.15) !important; }
/* Tennis's subtabs: smaller, under the sport row. */
.st-key-tennis_tabs, .st-key-all_tabs { margin-bottom: 0.3rem; }
.st-key-tennis_tabs a, .st-key-all_tabs a { padding: 0.25rem 0.8rem !important; }
.st-key-tennis_tabs a p, .st-key-all_tabs a p { font-size: 0.95rem !important; }
/* Phones: the three sport tabs stay on one row (user, 21 Nov 2026: Basketball wrapped
   under Tennis), smaller, and scroll sideways rather than wrap if still too wide. */
@media (max-width: 700px) {
    .st-key-sport_tabs, .st-key-tennis_tabs, .st-key-all_tabs {
        flex-wrap: nowrap !important;
        overflow-x: auto;
        scrollbar-width: none;
        gap: 0.25rem !important;
    }
    .st-key-sport_tabs > div, .st-key-tennis_tabs > div, .st-key-all_tabs > div {
        flex-shrink: 0;
        width: auto !important;
    }
    .st-key-sport_tabs a { padding: 0.35rem 0.6rem !important; }
    .st-key-sport_tabs a p,
    .st-key-sport_tabs a span { font-size: 1rem !important; }
    .st-key-tennis_tabs a, .st-key-all_tabs a { padding: 0.25rem 0.6rem !important; }
}
</style>"""


def main() -> None:
    import streamlit as st

    if st.query_params.get("page") == ABOUT_PAGE:
        st.set_page_config(page_title="About · Betting EV", layout="centered")
        # Cached so that the whole server, across tabs and reruns, starts one watcher.
        st.cache_resource(show_spinner=False)(start_browser_watch)()
        render_about(st)
        return

    st.set_page_config(page_title="Betting EV", layout="wide")
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

    # One tab per sport (user, 20 Nov 2026). Each has its own URL path and its own
    # sidebar; tennis stays the default at "/". Streamlit's own top navigation sits in
    # the fixed, see-through header, so the page scrolled under it: the tabs are page
    # links at the top of the content instead, and scroll away with it.
    # "All" comes first but tennis stays the default page (user, 20 Nov 2026).
    tennis = st.Page(lambda: render_tennis(st), title="Tennis",
                     icon=":material/sports_tennis:", url_path="tennis", default=True)
    # Prediction markets next to Pinnacle, comparison only (user, 20 Nov 2026): a
    # subtab of Tennis, so it is left out of the sport row.
    markets = st.Page(lambda: render_markets(st), title="Prediction markets",
                      icon=":material/query_stats:", url_path="tennis-markets")
    all_sports = st.Page(lambda: render_all(st), title="All", icon=":material/apps:",
                         url_path="all")
    sports = [
        all_sports,
        tennis,
        st.Page(lambda: render_basketball(st), title="Basketball",
                icon=":material/sports_basketball:", url_path="basketball"),
    ]
    page = st.navigation([*sports, markets], position="hidden")
    on_markets = page.title == markets.title
    st.html(SPORT_TABS_STYLE)
    with st.container(horizontal=True, key="sport_tabs", gap="small"):
        for each in sports:
            if each is tennis and on_markets:
                # Streamlit marks only the page shown; Tennis stays marked on its subtab.
                with st.container(key="sport_tab_parent", width="content"):
                    st.page_link(each)
            else:
                st.page_link(each)
    if on_markets or page.title == tennis.title:
        with st.container(horizontal=True, key="tennis_tabs", gap="small"):
            st.page_link(tennis, label="Sharp vs soft", icon=":material/compare_arrows:")
            st.page_link(markets)
    # All's subtabs (user, 20 Nov 2026): Pipeline health is the first and the default,
    # drawn by the All page itself, like Sharp vs soft on Tennis.
    if page.title == all_sports.title:
        with st.container(horizontal=True, key="all_tabs", gap="small"):
            st.page_link(all_sports, label="Pipeline health", icon=":material/monitor_heart:")
    page.run()


def render_all(st) -> None:
    """All's default subtab, Pipeline health: run and source success across every sport
    (dashboard_runs.py; user, 20 Nov 2026)."""
    from dashboard_runs import render_health

    render_health(st)


def render_markets(st) -> None:
    """Polymarket and Kalshi against Pinnacle and the books (dashboard_markets.py)."""
    from dashboard_markets import render_markets as render

    render(st)


def render_basketball(st) -> None:
    """Placeholder until the NBA view is designed; nba_collect.py already logs prices."""
    st.info(
        "Basketball is coming. NBA prices are already being collected "
        "(scripts/nba_collect.py); this tab will show them once it is designed."
    )


def render_tennis(st) -> None:
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
        # The session picker (render_session_picker) is off since runs every 5 minutes
        # put 288 sessions in a day (user, 15 Nov 2026): the page follows the newest
        # run. Its code stays, to bring it back once it scales.
        selected = state[SESSION_KEY] = LATEST

        resets = state.get(RESET_KEY, 0)
        if resets != state.get("settings_reset_sent", 0):
            state["settings_reset_sent"] = resets
            st.html(reset_script(resets), unsafe_allow_javascript=True)

        with st.container(border=True):
            with_ev = st.checkbox(
                "Show all matches with EV",
                key="with_ev",
                help="Only matches whose best EV is at least the minimum below. "
                "The overview then keeps only its Signals and Track record cards.",
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
                help="Green from here up. The Track record counts matches from here up.",
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
        with st.spinner("Reading Pinnacle snapshots and match results..."), SOURCES_LOCK:
            outcomes = load_outcome_sources(cache)

    # Worked out in a thread: until it is there the card says so and the header
    # checks every second, then asks for the full run that draws it.
    loader = st.cache_resource(show_spinner=False)(track_loader)()
    record = loader.request(filters["min_ev"], books, wait=TRACK_WAIT_SECONDS)
    track_loading = record is None and loader.error is None
    track = track_card(record, filters["min_ev"], books, loader.error)

    @st.fragment(run_every=REFRESH_SECONDS if running or track_loading else IDLE_REFRESH_SECONDS)
    def live_header() -> None:
        """A fragment leaves the rest of the page alone, so only a full run redraws
        the table, and this asks for one when the file has changed. So does a run
        starting or ending, because only a full run can change this fragment's pace,
        and the track record arriving."""
        header_session, newest_table = current_sessions(selected)
        fresh, signature = load_rows(st, newest_table) if newest_table else ([], None)

        still_running = bool(header_session) and is_running(header_session)
        track_arrived = track_loading and loader.ready(filters["min_ev"], books)

        if signature != drawn or still_running != running or track_arrived:
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
                track,
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
