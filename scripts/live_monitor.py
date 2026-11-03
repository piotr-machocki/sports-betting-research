"""
Live terminal monitor for the Betclic/Pinnacle research pipeline.

Reads only the current session's processed CSV from:
    data/results/sessions/<session_id>.csv

One row per match (latest observation), sorted by best EV, in the same colour
conventions as compare_odds.py. A plain-text session report with every
observation is written to --output.

Display
-------
ALL matches are shown; there is no row limit.

* Interactive terminal (default): the monitor runs in the terminal's
  alternate screen. Header, PINNACLE DATA QUALITY and the column header stay
  pinned (the statistics box only makes sense once Betclic odds are in, so
  it is shown in the final report, not during collection);
  the match table below them is a scrollable viewport:

      Up / Down (or k / j)   one row
      PgUp / PgDn (or b/f)   one page
      Home / End (or g / G)  first / last row

  Mouse-wheel scrolling works in terminals that translate the wheel into
  arrow keys on the alternate screen (most do). New observations keep
  updating the data and the ranking while you scroll.

  When the session finishes (or on Ctrl+C) the monitor leaves the alternate
  screen and prints the COMPLETE table into the normal terminal, so the whole
  list can be scrolled with the terminal's own scrollbar / mouse wheel.

* Non-interactive output (piped, or --plain): no live redraw; a progress line
  is printed when new observations arrive and the complete table is printed
  once at the end.

The CSV is re-read only when it changes; the screen is redrawn every second
so the clocks (elapsed time, data age) keep ticking.

When run_once.py finishes it stores a "performance" block in the status JSON;
the monitor appends it to the TXT report and prints it after the final table.

EV is shown for all four devig methods of the Pinnacle odds (ref = power,
odds ratio, Shin, proportional), always for the BET ON side; ranking
and statistics use EV(ref). After the run, run_once.py saves this table to
data/results/reports/ and the raw values to data/results/raw/ (see
results_store.py).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from perf_summary import performance_lines, quality_section
from results_store import (
    METHODS,
    REFERENCE_METHOD,
    complete_methods,
    ev_column,
    overround,
)

if os.name == "nt":
    import msvcrt
else:
    import select
    import termios
    import tty


ROOT = Path(__file__).resolve().parents[1]

SESSIONS_DIR = ROOT / "data" / "results" / "sessions"
SUPERBET_RAW_DIR = ROOT / "data" / "superbet" / "raw"

POLL_SECONDS = 1.0
KEY_POLL_SECONDS = 0.25
FINISHED_STATES = {"finished", "failed", "interrupted"}

# Warn when a running session has produced nothing for this long.
STUCK_AFTER_SECONDS = 90

# The live view tries to leave at least this many table rows visible and
# drops the legend when the window is short.
COMFORT_ROWS = 10

# While PINNACLE DATA QUALITY is shown it takes priority over the legend; the
# table only needs this many visible rows (it scrolls).
QUALITY_MIN_ROWS = 5

PW = 16  # player-name column width (shortened names, see short_name)
TOURNAMENT_W = 20  # header width only; rows are never cut (short_tournament)

# Table abbreviations of Pinnacle's "<prefix> <city> - <round>" tournament names.
TOURNAMENT_PREFIXES = {
    "atp challenger": "CH",
    "wta 125k": "W125",
    "itf men": "ITF M",
    "itf women": "ITF W",
}
TOURNAMENT_ROUNDS = {"Qualifiers": "Q", "Doubles": "D", "Final": "F"}

STATUS_MARKS = {"NOT FOUND": "N", "VERIFY": "V", "AMBIGUOUS": "A"}

LEGEND_LABEL_W = 12  # label column of the legend, see legend_row

# EV column header per devig method (betting.margin.METHODS order).
EV_LABELS = {
    "power": "EV(ref)",
    "odds_ratio": "EV(OR)",
    "shin": "EV(shin)",
    "proportional": "EV(prop)",
}

FIELDS = [
    "observation_id",
    "match_no",
    "pinnacle_captured_at",
    "betclic_captured_at",
    "observation_gap_seconds",
    "pinnacle_player1",
    "pinnacle_player2",
    "pinnacle_tournament",
    "pinnacle_odds1",
    "pinnacle_odds2",
    "probability1",
    "probability2",
    "betclic_player1",
    "betclic_player2",
    "betclic_odds_for_pin_p1",
    "betclic_odds_for_pin_p2",
    "ev1",
    "ev2",
    "best_ev",
    "best_player",
    "status",
    "confidence",
    "betclic_status",
    "url",
    "note",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor one session of EV calculations."
    )

    parser.add_argument(
        "--session-id",
        required=True,
        help="Unique ID of the current run.",
    )

    parser.add_argument(
        "--session-start",
        required=True,
        help="ISO timestamp marking the beginning of this run.",
    )

    parser.add_argument(
        "--status-file",
        required=True,
        type=Path,
        help="JSON file containing the runner's current status.",
    )

    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Destination for the session TXT report.",
    )

    parser.add_argument(
        "--top",
        type=int,
        default=None,
        help=(
            "Deprecated and ignored: every match is always displayed. "
            "Accepted so existing launchers keep working."
        ),
    )

    parser.add_argument(
        "--min-ev",
        type=float,
        default=0.02,
        help="Green/yellow colour threshold and positive-EV count.",
    )

    parser.add_argument(
        "--expected",
        type=int,
        default=None,
        help="Expected number of observations (enables progress bar and ETA).",
    )

    parser.add_argument(
        "--max-age",
        type=float,
        default=15.0,
        help=(
            "Minutes between the Pinnacle and Betclic captures after which "
            "its GAP turns yellow. Default: 15."
        ),
    )

    parser.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="auto",
        help="ANSI colours in the terminal table.",
    )

    parser.add_argument(
        "--plain",
        action="store_true",
        help=(
            "No interactive/alternate-screen view: print progress lines "
            "while running and the complete table at the end."
        ),
    )

    return parser.parse_args()


# ---------------------------------------------------------------- colours ---


class Style:
    enabled = False


def setup_color(mode: str) -> None:
    """Set terminal colour mode; auto honours TTY and NO_COLOR."""
    if mode == "always":
        Style.enabled = True
    elif mode == "never":
        Style.enabled = False
    else:
        Style.enabled = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    if Style.enabled and os.name == "nt":
        os.system("")  # enables ANSI escape processing in the Windows console


def paint(text: str, *codes: str) -> str:
    if not Style.enabled or not codes:
        return text

    return f"\033[{';'.join(codes)}m{text}\033[0m"


@contextmanager
def plain():
    """Render without ANSI codes (used for the TXT report)."""
    previous = Style.enabled
    Style.enabled = False

    try:
        yield
    finally:
        Style.enabled = previous


def ev_style(value: float, min_ev: float) -> tuple[str, ...]:
    """Same thresholds as compare_odds.py."""
    if value < 0:
        return ("31",)
    if value == 0:
        return ("90",)
    if value < min_ev:
        return ("33",)
    return ("1", "32")


STATUS_STYLE = {
    "running": ("1", "36"),
    "finished": ("1", "32"),
    "failed": ("1", "31"),
    "interrupted": ("1", "33"),
}


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def clip(line: str, width: int) -> str:
    """Cut a line to `width` visible characters, ignoring ANSI sequences.

    Keeps every table row on exactly one terminal line, which the scrolling
    viewport relies on; a wrapped row would shift everything below it.
    """
    if width <= 0:
        return line

    out: list[str] = []
    visible = 0
    i = 0
    cut = False

    while i < len(line):
        match = _ANSI.match(line, i)

        if match:
            out.append(match.group())
            i = match.end()
            continue

        if visible >= width:
            cut = True
            break

        out.append(line[i])
        visible += 1
        i += 1

    text = "".join(out)

    if cut and Style.enabled:
        text += "\033[0m"

    return text


# ---------------------------------------------------------------- parsing ---


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None

    try:
        result = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)

        return result.astimezone(timezone.utc)

    except (ValueError, TypeError):
        return None


def number(value: Any) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None

        return float(value)

    except (TypeError, ValueError):
        return None


def match_key(row: dict) -> tuple:
    return (
        row.get("pinnacle_player1", ""),
        row.get("pinnacle_player2", ""),
        row.get("pinnacle_tournament", ""),
    )


def read_superbet(session_id: str) -> dict[tuple, dict]:
    """This session's Superbet matches, if it was collected. Never fails a run.

    VERIFY rows have prices but no EV, so they show without affecting BET ON,
    the sort or the counts.
    """
    path = SUPERBET_RAW_DIR / f"superbet_{session_id}.csv"

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            return {
                match_key(row): row
                for row in csv.DictReader(file)
                if row.get("status") in ("FOUND", "VERIFY")
            }
    except (OSError, csv.Error):
        return {}


def add_superbet(row: dict, superbet: dict[tuple, dict]) -> dict:
    """superbet_* cells on a session row: prices, net odds and their EV."""
    found = superbet.get(match_key(row))

    if found is None:
        return row

    return {
        **row,
        "superbet_status": found.get("status", ""),
        "superbet_player1": found.get("superbet_player1", ""),
        "superbet_player2": found.get("superbet_player2", ""),
        "superbet_price1": found.get("superbet_price1", ""),
        "superbet_price2": found.get("superbet_price2", ""),
        "superbet_odds1": found.get("superbet_odds1", ""),
        "superbet_odds2": found.get("superbet_odds2", ""),
        "superbet_ev1": found.get("ev1", ""),
        "superbet_ev2": found.get("ev2", ""),
        "superbet_best_ev": found.get("best_ev", ""),
    }


def has_superbet(rows: list[dict]) -> bool:
    return any(row.get("superbet_odds1") for row in rows)


def superbet_odds_cell(row: dict, side: int) -> str:
    """Net odds in the 5-wide column; an unconfirmed match ends in a yellow '?'."""
    # Net odds have up to 4 decimals; the column holds 2.
    net_odds = number(row.get(f"superbet_odds{side}"))

    if net_odds is None:
        return f"{'--':>5}"

    if row.get("superbet_status") != "VERIFY":
        return f"{net_odds:.2f}".rjust(5)

    return paint(f"{net_odds:.2f}"[:4].rstrip(".").rjust(4) + "?", "33")


def read_session_rows(path: Path) -> list[dict[str, str]] | None:
    """
    Read only this session's processed CSV.

    Returns None on a transient read error (the worker may be writing
    simultaneously) so the caller keeps its previous rows and retries.
    Truncated rows (a partially written last line) are dropped.
    """

    if not path.exists():
        return []

    try:
        with path.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as file:
            rows = list(csv.DictReader(file))

    except (OSError, csv.Error):
        return None

    # complete_methods: sessions processed before the per-method columns
    # existed get them recalculated from the stored odds.
    superbet = read_superbet(path.stem)

    return [
        add_superbet(complete_methods(row), superbet)
        for row in rows
        if row.get("observation_id")
        and None not in row
        and None not in row.values()
    ]


def read_runner_info(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    except (OSError, json.JSONDecodeError):
        return {}


# ------------------------------------------------------------- formatting ---


def session_clock(info: dict, now: datetime) -> datetime:
    """A finished session is measured up to its end, not up to "now"."""
    if str(info.get("status", "")).lower() in FINISHED_STATES:
        return parse_timestamp(info.get("session_finished_at")) or now

    return now


def pinnacle_info(info: dict) -> dict:
    """Pinnacle collection stats from the runner status file.

    The final "performance" block wins; before it exists the runner's early
    "pinnacle" block (written before Betclic collection starts) is used.
    """
    performance = info.get("performance") or {}

    return performance.get("pinnacle") or info.get("pinnacle") or {}


def early_quality_lines(info: dict) -> list[str]:
    """PINNACLE DATA QUALITY for a report written before the final summary.

    Once the final performance block exists, performance_lines prints the
    section itself, so it is never listed twice.
    """
    if info.get("performance"):
        return []

    return quality_section(pinnacle_info(info))


def sort_key(row: dict) -> tuple:
    evs = [
        ev
        for ev in (number(row.get("best_ev")), number(row.get("superbet_best_ev")))
        if ev is not None
    ]

    if not evs:
        return (1, 0.0)

    return (0, -max(evs))


def format_probability(value: Any) -> str:
    parsed = number(value)

    if parsed is None:
        return "--"

    return f"{parsed * 100:.2f}%"


def format_ev(value: Any) -> str:
    parsed = number(value)

    if parsed is None:
        return "--"

    return f"{parsed:+.2%}"


def format_odds(value: Any) -> str:
    """At least 2, at most 4 decimals (same as compare_odds.py)."""
    parsed = number(value)

    if parsed is None:
        return "--"

    text = f"{parsed:.4f}".rstrip("0")
    whole, decimals = text.split(".")

    return f"{whole}.{decimals.ljust(2, '0')}"


def fmt_age(seconds: float) -> str:
    seconds = max(0, round(seconds))

    if seconds < 60:
        return f"{seconds} s"

    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60:02d} s"

    return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"


def fmt_gap(seconds: float) -> str:
    """Compact fmt_age for the table: '19s', '1m05s', '1h02m'."""
    seconds = max(0, round(seconds))

    if seconds < 60:
        return f"{seconds}s"

    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"

    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def short_name(value: Any) -> str:
    """'Carlos Alcaraz' -> 'C. Alcaraz'; doubles: 'Krajicek/Mektic', with the
    surnames cut to share the name column ('Kucmova/Laboutko')."""
    players = [part.split() for part in str(value or "").split("/")]
    players = [tokens for tokens in players if tokens]

    if not players:
        return "?"

    if len(players) > 1:
        first, second = players[0][-1], players[-1][-1]
        room = PW - 1
        first = first[:max(room // 2, room - len(second))]
        return f"{first}/{second[:room - len(first)]}"

    tokens = players[0]

    if len(tokens) == 1:
        return tokens[0]

    return f"{tokens[0][0]}. {tokens[-1]}"


def short_tournament(value: Any) -> str:
    """'ATP Challenger Bratislava - Qualifiers' -> 'CH Bratislava Q'."""
    name, separator, stage = str(value or "").strip().rpartition(" - ")

    if not separator:
        name, stage = stage, ""

    for prefix, short in TOURNAMENT_PREFIXES.items():
        if name.lower().startswith(prefix + " "):
            name = short + name[len(prefix):]
            break

    return f"{name} {TOURNAMENT_ROUNDS.get(stage, stage)}".strip()


def format_margin(odds1: Any, odds2: Any) -> str:
    margin = overround(odds1, odds2)

    if margin is None:
        return "--"

    return f"{margin * 100:.2f}%"


def format_net_margin(odds1: Any, odds2: Any) -> str:
    """Margin of prices after tax; unlike overround it allows odds below 1.0."""
    first, second = number(odds1), number(odds2)

    if not first or not second:
        return "--"

    return f"{(1 / first + 1 / second - 1) * 100:.2f}%"


def match_name(row: dict) -> str:
    return (
        f"{row.get('pinnacle_player1') or '?'} vs "
        f"{row.get('pinnacle_player2') or '?'}"
    )


# ------------------------------------------------------------- row logic ----


def best_side(row: dict) -> int | None:
    """1 or 2 = Pinnacle player order; None when no EV was calculated.

    Mirrors process_live_odds.py: side 1 wins ties.
    """
    ev1 = number(row.get("ev1"))
    ev2 = number(row.get("ev2"))

    if ev1 is None or ev2 is None:
        return None

    return 1 if ev1 >= ev2 else 2


def table_side(row: dict) -> int | None:
    """The BET ON side: the best EV of either book, side 1 on a tie."""
    best = best_side(row)
    ev1 = number(row.get("superbet_ev1"))
    ev2 = number(row.get("superbet_ev2"))

    if ev1 is None or ev2 is None:
        return best

    superbet = 1 if ev1 >= ev2 else 2

    if best is None:
        return superbet

    return superbet if max(ev1, ev2) > number(row.get(f"ev{best}")) else best


def latest_per_match(rows: list[dict]) -> list[dict]:
    """One row per match: the most recent Betclic observation.

    Normally the scraper emits one observation per match and this is a no-op;
    if a match is observed twice, only the newest one is shown.
    """
    newest: dict[tuple, tuple[float, dict]] = {}

    for row in rows:
        key = match_key(row)

        stamp = parse_timestamp(row.get("betclic_captured_at"))
        value = stamp.timestamp() if stamp else float("-inf")

        if key not in newest or value >= newest[key][0]:
            newest[key] = (value, row)

    return [row for _, row in newest.values()]


def is_stale(row: dict, max_age_minutes: float) -> bool:
    gap = number(row.get("observation_gap_seconds"))
    return gap is not None and gap > max_age_minutes * 60


def status_mark(row: dict) -> str:
    """One letter saying why a match has no EV; blank when it has one."""
    if best_side(row) is not None:
        return " "

    status = str(row.get("status") or "?")

    if status == "ERROR":
        return paint("E", "1", "31")

    if status != "FOUND":
        return paint(STATUS_MARKS.get(status, "?"), "1", "33")

    if str(row.get("betclic_status", "")).lower() == "live":
        return paint("L", "1", "33")

    return paint("X", "90")


# ---------------------------------------------------------------- table -----


def table_row(cells: list[str]) -> str:
    return paint("|", "90").join(cells)


def table_methods(superbet: bool, compact: bool) -> list[str]:
    """Superbet's columns make the terminal table too wide for all four EVs."""
    return [REFERENCE_METHOD] if superbet and compact else list(METHODS)


def table_header_lines(superbet: bool = False, compact: bool = False) -> list[str]:
    ev_labels = [
        "EV(Bc)" if superbet and method == REFERENCE_METHOD else EV_LABELS[method]
        for method in table_methods(superbet, compact)
    ]

    cells = [
        f"{'#':>3}  ",
        f" {'BET ON':<{PW}}",
        f" {'OPPONENT':<{PW}}",
        f"{'Betc':>5} ",
        *([f"{'Supb':>5} "] if superbet else []),
        f"{'Pinn':>6} ",
        f"{'Mb':>6} ",
        *([f"{'Ms':>6} "] if superbet else []),
        f"{'Mp':>5} ",
        f"{'P(ref)':>6} ",
        *(f"{label:^8}" for label in ev_labels),
        *([f"{'EV(Sb)':^8}"] if superbet else []),
        f"{'GAP':>5} ",
        f" {'CONF':<6}",
        f" {'TOURNAMENT':<{TOURNAMENT_W}}",
    ]

    return [
        table_row([paint(cell, "1") for cell in cells]),
        "+".join("-" * len(cell) for cell in cells),
    ]


def ev_cell(value: float | None, min_ev: float) -> str:
    if value is None:
        return paint(f"{'--':>7}", "2")

    return paint(f"{value * 100:>+6.2f}%", *ev_style(value, min_ev))


def table_row_lines(
    rows: list[dict],
    args: argparse.Namespace,
    compact: bool = False,
) -> list[str]:
    """One terminal line per match. Numbering is the absolute rank.

    BET ON is the side with the best reference EV; every EV column shows
    that same side under a different devig method. With Superbet data the
    side is the best of either book and its columns are added.
    """
    if not rows:
        return [paint("  waiting for Betclic observations...", "2")]

    lines = []
    superbet = has_superbet(rows)
    methods = table_methods(superbet, compact)

    for index, row in enumerate(rows, start=1):
        side = table_side(row)

        if side is None:
            name = short_name(row.get("pinnacle_player1"))
            opponent = short_name(row.get("pinnacle_player2"))

            name_style = ("2",)
            betclic = f"{'--':>5}"
            pinnacle = probability = f"{'--':>6}"
            ev_cells = [ev_cell(None, args.min_ev) for _ in methods]
            superbet_odds = f"{'--':>5}"
            superbet_ev = ev_cell(None, args.min_ev)

        else:
            other = 2 if side == 1 else 1

            name = short_name(row.get(f"pinnacle_player{side}"))
            opponent = short_name(row.get(f"pinnacle_player{other}"))

            name_style = ("1", "36")
            betclic = format_odds(
                row.get(f"betclic_odds_for_pin_p{side}")
            ).rjust(5)
            pinnacle = format_odds(
                row.get(f"pinnacle_odds{side}")
            ).rjust(6)
            probability = format_probability(
                row.get(f"probability{side}")
            ).rjust(6)
            ev_cells = [
                ev_cell(number(row.get(ev_column(method, side))), args.min_ev)
                for method in methods
            ]
            superbet_odds = superbet_odds_cell(row, side)
            superbet_ev = ev_cell(
                number(row.get(f"superbet_ev{side}")),
                args.min_ev,
            )

        gap = number(row.get("observation_gap_seconds"))
        gap_text = fmt_gap(gap) if gap is not None else "--"
        gap_cell = paint(
            f"{gap_text:>5}",
            *(("33",) if is_stale(row, args.max_age) else ("2",)),
        )

        confidence = f"{str(row.get('confidence') or '')[:6]:<6}"

        # Margins cover the whole market, so they need no BET ON side.
        betclic_margin = format_margin(
            row.get("betclic_odds_for_pin_p1"),
            row.get("betclic_odds_for_pin_p2"),
        ).rjust(6)
        pinnacle_margin = format_margin(
            row.get("pinnacle_odds1"),
            row.get("pinnacle_odds2"),
        ).rjust(5)
        superbet_margin = format_net_margin(
            row.get("superbet_odds1"),
            row.get("superbet_odds2"),
        ).rjust(6)

        # Numbers keep a space before the next bar, text a space after it.
        lines.append(table_row([
            f"{index:>3} {status_mark(row)}",
            " " + paint(f"{name[:PW]:<{PW}}", *name_style),
            " " + paint(f"{opponent[:PW]:<{PW}}", "2"),
            *(
                f"{cell} "
                for cell in (
                    betclic,
                    *([superbet_odds] if superbet else []),
                    pinnacle,
                    betclic_margin,
                    *([superbet_margin] if superbet else []),
                    pinnacle_margin,
                    probability,
                    *ev_cells,
                    *([superbet_ev] if superbet else []),
                    gap_cell,
                )
            ),
            f" {confidence}",
            f" {short_tournament(row.get('pinnacle_tournament'))}",
        ]))

    return lines


def table_lines(
    rows: list[dict],
    args: argparse.Namespace,
    compact: bool = False,
) -> list[str]:
    return (
        table_header_lines(has_superbet(rows), compact)
        + table_row_lines(rows, args, compact)
    )


# ------------------------------------------------------------ statistics ----


def stats_items(
    all_rows: list[dict],
    latest: list[dict],
    args: argparse.Namespace,
) -> list[tuple[str, str, str]]:
    """(long label, short label, value) for every statistic."""

    def count(predicate) -> int:
        return sum(1 for row in latest if predicate(row))

    evs = [number(row.get("best_ev")) for row in latest]

    processed = (
        f"{len(all_rows)} / {args.expected}"
        if args.expected
        else str(len(all_rows))
    )

    superbet_evs = [number(row.get("superbet_best_ev")) for row in latest]
    superbet_items = [
        ("Superbet found", "SbFound", str(sum(ev is not None for ev in superbet_evs))),
        (
            "Superbet EV(ref) > 0%",
            "SbEV>0",
            str(sum(ev is not None and ev > 0 for ev in superbet_evs)),
        ),
    ] if has_superbet(latest) else []

    return [
        ("Observations processed", "Obs", processed),
        ("Unique matches", "Matches", str(len(latest))),
        (
            "Found",
            "Found",
            str(count(lambda r: r.get("status") == "FOUND")),
        ),
        (
            "Not found",
            "NotFound",
            str(count(lambda r: r.get("status") == "NOT FOUND")),
        ),
        (
            "Errors",
            "Err",
            str(count(lambda r: r.get("status") == "ERROR")),
        ),
        (
            "FOUND but no EV (live etc.)",
            "NoEV",
            str(
                count(
                    lambda r: r.get("status") == "FOUND"
                    and number(r.get("best_ev")) is None
                )
            ),
        ),
        (
            "EV calculated",
            "EVcalc",
            str(sum(ev is not None for ev in evs)),
        ),
        (
            "EV(ref) > 0%",
            "EV>0",
            str(sum(ev is not None and ev > 0 for ev in evs)),
        ),
        (
            f"EV(ref) >= {args.min_ev:.2%}",
            f"EV>={args.min_ev:.0%}",
            str(sum(ev is not None and ev >= args.min_ev for ev in evs)),
        ),
        (
            f"Stale (gap > {args.max_age:g} min)",
            "Stale",
            str(count(lambda r: is_stale(r, args.max_age))),
        ),
        *superbet_items,
    ]


def stats_box(
    all_rows: list[dict],
    latest: list[dict],
    args: argparse.Namespace,
) -> list[str]:
    items = [
        (label, value)
        for label, _, value in stats_items(all_rows, latest, args)
    ]

    left, right = items[0::2], items[1::2]
    right += [("", "")] * (len(left) - len(right))

    widths = [
        max(len(k) for k, _ in left + [("Metric", "")]),
        max(len(v) for _, v in left + [("", "Value")]),
        max(len(k) for k, _ in right + [("Metric", "")]),
        max(len(v) for _, v in right + [("", "Value")]),
    ]

    separator = "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def line(cells, style=()):
        return "|" + "|".join(
            " " + paint(cell.ljust(width), *style) + " "
            for cell, width in zip(cells, widths)
        ) + "|"

    lines = [
        separator,
        line(("Metric", "Value", "Metric", "Value"), ("1",)),
        separator,
    ]

    for (k1, v1), (k2, v2) in zip(left, right):
        lines.append(line((k1, v1, k2, v2)))

    lines.append(separator)
    return lines


def header_lines(
    all_rows: list[dict],
    args: argparse.Namespace,
    session_start: datetime,
    info: dict,
    now: datetime,
) -> list[str]:
    status = str(info.get("status", "running")).lower()
    finished = status in FINISHED_STATES

    clock = session_clock(info, now)

    elapsed = (clock - session_start).total_seconds()
    processed = len(all_rows)

    betclic_times = [
        t for t in (
            parse_timestamp(row.get("betclic_captured_at"))
            for row in all_rows
        ) if t
    ]
    pinnacle_times = [
        t for t in (
            parse_timestamp(row.get("pinnacle_captured_at"))
            for row in all_rows
        ) if t
    ]
    gaps = [
        gap for gap in (
            number(row.get("observation_gap_seconds")) for row in all_rows
        ) if gap is not None
    ]

    pinnacle_age = (clock - (min(pinnacle_times) if pinnacle_times else session_start)).total_seconds()

    last_text = "none yet"
    idle = elapsed

    if betclic_times:
        idle = (clock - max(betclic_times)).total_seconds()
        last_text = f"{fmt_age(idle)} ago"

    lines = [
        (
            paint("SPORTS BETTING RESEARCH — LIVE SESSION", "1")
            + "   "
            + paint(status.upper(), *STATUS_STYLE.get(status, ("1",)))
        ),
        f"Session: {args.session_id}",
    ]

    if args.expected:
        fraction = min(1.0, processed / args.expected)
        filled = round(20 * fraction)
        eta = ""

        if not finished and 0 < processed < args.expected and elapsed > 0:
            remaining = elapsed / processed * (args.expected - processed)
            eta = f"   ETA ~{fmt_age(remaining)}"

        lines.append(
            f"Progress: [{'#' * filled}{'-' * (20 - filled)}] "
            f"{processed}/{args.expected} ({fraction:.0%}){eta}"
        )

    return lines

def legend_row(label: str, *items: str, styles: tuple[str, ...] = ("1",)) -> str:
    """One topic per line: a bold label column, then its items."""
    return (
        f"  {paint(f'{label:<{LEGEND_LABEL_W}}', *styles)}  "
        + paint(" | ", "90").join(items)
    )


def legend_lines(args: argparse.Namespace, superbet: bool = False) -> list[str]:
    threshold = f"{args.min_ev * 100:g}%"

    return [
        paint("Legend:", "1"),
        legend_row(
            "BET ON",
            "side with the best EV(ref)",
            "every EV column is for this side",
            styles=("1", "36"),
        ),
        legend_row(
            "Rows",
            "one per match (latest observation)",
            "best EV(ref) first",
        ),
        legend_row("Betc / Pinn", "Betclic / Pinnacle odds"),
        legend_row("Mb / Mp", "Betclic / Pinnacle margin (overround)"),
        *(
            [
                legend_row(
                    "Supb / Ms",
                    "Superbet odds / margin after its 12% tax",
                    "EV(Sb) = its power EV",
                    "shown prices are in the report",
                    "? = unconfirmed match, no EV",
                ),
            ]
            if superbet
            else []
        ),
        legend_row(
            "Devig",
            "P(ref) / EV(ref) = power",
            "OR = odds ratio",
            "shin = Shin",
            "prop = proportional",
        ),
        legend_row(
            "EV colours",
            paint("red", "31") + " < 0%",
            paint("gray", "90") + " = 0%",
            paint("yellow", "33") + f" 0% to {threshold}",
            paint("green", "1", "32") + f" >= {threshold}",
        ),
        legend_row(
            "GAP",
            "Betclic capture minus Pinnacle capture",
            paint("yellow", "33") + f" = over {args.max_age:g} min (stale)",
        ),
        legend_row("CONF", "scraper match confidence"),
        legend_row(
            "No-EV letter",
            paint("N", "1", "33") + " not found",
            paint("V", "1", "33") + " verify",
            paint("A", "1", "33") + " ambiguous",
            paint("L", "1", "33") + " live",
            paint("E", "1", "31") + " error",
            paint("X", "90") + " other",
        ),
        legend_row(
            "TOURNAMENT",
            "CH = ATP Challenger",
            "W125 = WTA 125k",
            "ITF M/W = ITF Men/Women",
        ),
        legend_row("Round", "Q = qualifiers", "D = doubles", "F = final"),
    ]



# ------------------------------------------------------- complete screens ---


def static_lines(
    all_rows: list[dict],
    args: argparse.Namespace,
    session_start: datetime,
    info: dict,
    now: datetime,
    compact: bool = False,
) -> list[str]:
    """The complete dashboard with EVERY match (report, final dump, --plain)."""
    latest = sorted(latest_per_match(all_rows), key=sort_key)
    clock = session_clock(info, now)

    return (
        header_lines(all_rows, args, session_start, info, now)
        + [""]
        + performance_lines(
            info.get("performance"),
            started_at=session_start,
            pinnacle_data_age_seconds=(
                clock
                - (
                    min(
                        (
                            t
                            for t in (
                                parse_timestamp(row.get("pinnacle_captured_at"))
                                for row in all_rows
                            )
                            if t is not None
                        ),
                        default=session_start,
                    )
                )
            ).total_seconds(),
        )
        + early_quality_lines(info)
        + [""]
        + stats_box(all_rows, latest, args)
        + [""]
        + table_lines(latest, args, compact)
        + [""]
        + legend_lines(args, has_superbet(latest))
    )



# ----------------------------------------------------------- live viewport --


@dataclass
class ViewState:
    """Scroll position of the live table (rows above the viewport)."""

    offset: int = 0
    body_h: int = COMFORT_ROWS
    max_offset: int = 0


def live_frame(
    all_rows: list[dict],
    args: argparse.Namespace,
    session_start: datetime,
    info: dict,
    now: datetime,
    term_lines: int,
    term_cols: int,
    offset: int,
) -> tuple[list[str], int, int, int]:
    """Build one screen: pinned header + scrollable table + pinned footer.

    Returns (lines, clamped_offset, viewport_height, max_offset). Every match
    is part of the scrollable table; nothing is cut off.
    """
    latest = sorted(latest_per_match(all_rows), key=sort_key)

    head = header_lines(all_rows, args, session_start, info, now)
    superbet = has_superbet(latest)
    legend = legend_lines(args, superbet)
    thead = table_header_lines(superbet, compact=True)

    # The statistics box (found / not found / EV counts) is not part of the
    # live screen: it only means something once the Betclic odds are in, so it
    # appears in the final report (static_lines) after collection.
    layouts = [
        (head + [""] + thead, legend, COMFORT_ROWS),
        (head + [""] + thead, legend[:1], COMFORT_ROWS),
        (head + [""] + thead, [], COMFORT_ROWS),
    ]

    # PINNACLE DATA QUALITY is known before Betclic collection starts, so it is
    # pinned under the header from the first frame and stays there, ahead of
    # the legend.
    quality = quality_section(pinnacle_info(info))

    if quality:
        with_quality = head + [""] + quality + [""]

        layouts = [
            (with_quality + thead, legend, COMFORT_ROWS),
            (with_quality + thead, legend[:1], COMFORT_ROWS),
            (with_quality + thead, [], QUALITY_MIN_ROWS),
        ] + layouts

    usable = max(1, term_lines - 1)

    for top, bottom_legend, needed in layouts:
        # 1 line = the scroll status line under the table.
        body_h = usable - len(top) - len(bottom_legend) - 1

        if body_h >= needed:
            break

    body_h = max(3, body_h)

    row_lines = table_row_lines(latest, args, compact=True)
    total = len(row_lines) if latest else 0

    max_offset = max(0, total - body_h)
    offset = min(max(0, offset), max_offset)

    body = row_lines[offset: offset + body_h]
    body += [""] * (body_h - len(body))

    if total > body_h:
        first = offset + 1
        last = min(total, offset + body_h)
        hints = []

        if offset > 0:
            hints.append("more above")

        if offset < max_offset:
            hints.append("more below")

        status_line = paint(
            f"Rows {first}-{last} of {total}"
            + (f" ({', '.join(hints)})" if hints else "")
            + "   Up/Down scroll | PgUp/PgDn page | Home/End jump "
            "| Ctrl+C stop",
            "1",
            "35",
        )
    else:
        status_line = paint(f"All {total} matches shown.", "2")

    lines = top + body + [status_line] + bottom_legend

    return lines, offset, body_h, max_offset


# ------------------------------------------------------------------ keys ----

CHAR_KEYS = {
    "j": "down",
    "k": "up",
    " ": "pgdn",
    "f": "pgdn",
    "b": "pgup",
    "g": "home",
    "G": "end",
}

ARROW_LETTERS = {"A": "up", "B": "down", "H": "home", "F": "end"}
TILDE_KEYS = {"1": "home", "4": "end", "5": "pgup", "6": "pgdn"}
WINDOWS_KEYS = {
    "H": "up",
    "P": "down",
    "I": "pgup",
    "Q": "pgdn",
    "G": "home",
    "O": "end",
}

_KEY_RE = re.compile(
    r"\x1b\[(?:1;\d)?([ABHF])"  # CSI A/B/H/F (also with modifiers)
    r"|\x1bO([ABHF])"           # SS3 A/B/H/F (application cursor mode)
    r"|\x1b\[([1456])~"         # CSI n ~
    r"|(.)",
    re.DOTALL,
)


def parse_keys(data: str) -> list[str]:
    """Translate raw terminal input into key names (one call may hold many,
    e.g. a mouse-wheel burst arrives as several arrow sequences)."""
    keys = []

    for match in _KEY_RE.finditer(data):
        letter = match.group(1) or match.group(2)

        if letter:
            key = ARROW_LETTERS.get(letter)
        elif match.group(3):
            key = TILDE_KEYS.get(match.group(3))
        else:
            key = CHAR_KEYS.get(match.group(4))

        if key:
            keys.append(key)

    return keys


class KeyReader:
    """Non-blocking keyboard input for the scroll keys."""

    def __init__(self) -> None:
        self.fd: int | None = None
        self.saved = None

    def start(self) -> bool:
        if os.name == "nt":
            return True

        try:
            self.fd = sys.stdin.fileno()
            self.saved = termios.tcgetattr(self.fd)
            # cbreak keeps Ctrl+C working (ISIG stays on).
            tty.setcbreak(self.fd)
            return True

        except (termios.error, OSError, ValueError):
            self.fd = None
            self.saved = None
            return False

    def stop(self) -> None:
        if os.name != "nt" and self.fd is not None and self.saved is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
            except (termios.error, OSError):
                pass

    def poll(self, timeout: float) -> list[str]:
        if os.name == "nt":
            deadline = time.monotonic() + timeout
            keys: list[str] = []

            while True:
                while msvcrt.kbhit():
                    char = msvcrt.getwch()

                    if char in ("\x00", "\xe0"):
                        key = WINDOWS_KEYS.get(msvcrt.getwch())
                    else:
                        key = CHAR_KEYS.get(char)

                    if key:
                        keys.append(key)

                if keys or time.monotonic() >= deadline:
                    return keys

                time.sleep(0.02)

        try:
            ready, _, _ = select.select([self.fd], [], [], timeout)

            if not ready:
                return []

            data = os.read(self.fd, 256).decode("utf-8", "ignore")

        except (OSError, ValueError):
            time.sleep(timeout)
            return []

        return parse_keys(data)


def apply_keys(view: ViewState, keys: list[str]) -> bool:
    """Update the scroll position; True when it may have changed."""
    page = max(1, view.body_h - 1)

    for key in keys:
        if key == "up":
            view.offset -= 1
        elif key == "down":
            view.offset += 1
        elif key == "pgup":
            view.offset -= page
        elif key == "pgdn":
            view.offset += page
        elif key == "home":
            view.offset = 0
        elif key == "end":
            view.offset = 10**9  # clamped to the last page by live_frame

    view.offset = max(0, view.offset)

    return bool(keys)


# ----------------------------------------------------------------- report ---


def render_report(
    all_rows: list[dict],
    args: argparse.Namespace,
    session_start: datetime,
    info: dict,
    now: datetime,
) -> str:
    with plain():
        lines = static_lines(all_rows, args, session_start, info, now)

    lines.extend([
        "",
        "=" * 100,
        f"Report generated: {now.isoformat(timespec='seconds')}",
        "P1/P2 follow Pinnacle player order. Betclic odds are mapped to",
        "that same player order. The table above shows the latest",
        "observation per match (all matches); details below cover",
        "every observation.",
    ])

    sorted_rows = sorted(all_rows, key=sort_key)

    if sorted_rows:
        lines.extend([
            "",
            "=" * 100,
            "OBSERVATION DETAILS",
            "=" * 100,
        ])

        for row in sorted_rows:
            lines.extend([
                "",
                f"Observation ID: {row.get('observation_id', '')}",
                f"Match #: {row.get('match_no', '')}",
                f"Match: {match_name(row)}",
                f"Tournament: {row.get('pinnacle_tournament', '')}",
                f"Status: {row.get('status', '')}",
                f"Confidence: {row.get('confidence', '')}",
                f"Pinnacle captured: {row.get('pinnacle_captured_at', '')}",
                f"Betclic captured: {row.get('betclic_captured_at', '')}",
                f"Observation gap (seconds): {row.get('observation_gap_seconds', '')}",
                (
                    "Pinnacle odds: "
                    f"{format_odds(row.get('pinnacle_odds1'))} / "
                    f"{format_odds(row.get('pinnacle_odds2'))}"
                ),
                (
                    "Fair probabilities: "
                    f"{format_probability(row.get('probability1'))} / "
                    f"{format_probability(row.get('probability2'))}"
                ),
                (
                    "Betclic odds: "
                    f"{format_odds(row.get('betclic_odds_for_pin_p1'))} / "
                    f"{format_odds(row.get('betclic_odds_for_pin_p2'))}"
                ),
                *(
                    f"{EV_LABELS[method]}: "
                    f"{format_ev(row.get(ev_column(method, 1)))} / "
                    f"{format_ev(row.get(ev_column(method, 2)))}"
                    for method in METHODS
                ),
                *(
                    [
                        *(
                            [
                                "Superbet match UNCONFIRMED: "
                                f"{row.get('superbet_player1', '')} vs "
                                f"{row.get('superbet_player2', '')}"
                            ]
                            if row.get("superbet_status") == "VERIFY"
                            else []
                        ),
                        (
                            "Superbet odds as shown: "
                            f"{format_odds(row.get('superbet_price1'))} / "
                            f"{format_odds(row.get('superbet_price2'))}"
                        ),
                        (
                            "Superbet odds after tax: "
                            f"{format_odds(row.get('superbet_odds1'))} / "
                            f"{format_odds(row.get('superbet_odds2'))}"
                        ),
                        (
                            "Superbet EV(ref): "
                            f"{format_ev(row.get('superbet_ev1'))} / "
                            f"{format_ev(row.get('superbet_ev2'))}"
                        ),
                    ]
                    if row.get("superbet_odds1")
                    else []
                ),
                f"Best EV: {format_ev(row.get('best_ev'))}",
                f"Best player: {row.get('best_player', '')}",
                f"Betclic status: {row.get('betclic_status', '')}",
                f"URL: {row.get('url', '')}",
                f"Note: {row.get('note', '')}",
            ])

    lines.extend([
        "",
        "=" * 100,
        "END OF SESSION REPORT",
        "=" * 100,
    ])

    return "\n".join(lines) + "\n"


def render_display_report(
    all_rows: list[dict],
    args: argparse.Namespace,
    session_start: datetime,
    info: dict,
    now: datetime,
) -> str:
    """The final terminal screen as plain text: every match, every EV column.

    Saved by results_store.py under data/results/reports/. Unlike
    render_report it has no per-observation details (the raw data has them).
    """
    with plain():
        lines = static_lines(all_rows, args, session_start, info, now)

    lines.extend([
        "",
        f"Report generated: {now.isoformat(timespec='seconds')}",
    ])

    return "\n".join(lines) + "\n"


def write_report(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


# ------------------------------------------------------------------ screen --

ALT_SCREEN_ON = "\033[?1049h\033[?25l\033[?1007h"
ALT_SCREEN_OFF = "\033[?1007l\033[?25h\033[?1049l"


def draw(lines: list[str], cols: int, clear: bool) -> None:
    """Redraw the alternate screen in place (it has no scrollback to lose)."""
    body = "\033[K\n".join(clip(line, cols - 1) for line in lines)

    sys.stdout.write(
        ("\033[2J" if clear else "") + "\033[H" + body + "\033[K\033[J"
    )
    sys.stdout.flush()


def main() -> None:
    args = parse_args()

    if not 0 <= args.min_ev:
        raise SystemExit("--min-ev must be non-negative")

    if args.expected is not None and args.expected < 1:
        raise SystemExit("--expected must be at least 1")

    if args.max_age <= 0:
        raise SystemExit("--max-age must be positive")

    session_start = parse_timestamp(args.session_start)

    if session_start is None:
        raise SystemExit("Invalid --session-start timestamp")

    setup_color(args.color)

    interactive = (
        not args.plain
        and sys.stdout.isatty()
        and sys.stdin.isatty()
    )

    keys: KeyReader | None = None

    if interactive:
        if os.name == "nt":
            os.system("")  # enable ANSI cursor control in the console

        keys = KeyReader()

        if not keys.start():
            interactive = False
            keys = None

    session_csv = SESSIONS_DIR / f"{args.session_id}.csv"

    args.output = args.output.resolve()
    args.status_file = args.status_file.resolve()

    rows: list[dict] = []
    csv_signature = None
    report_signature = None
    info: dict = {}
    status = "running"
    finished = False

    view = ViewState()
    last_size: tuple[int, int] | None = None
    last_plain_count = -1
    quality_printed = False

    if interactive:
        sys.stdout.write(ALT_SCREEN_ON)
        sys.stdout.flush()

    next_refresh = 0.0
    dirty = True

    try:
        while True:
            if time.monotonic() >= next_refresh:
                next_refresh = time.monotonic() + POLL_SECONDS
                dirty = True

                # Read the runner status BEFORE the CSV. The runner only
                # marks the session finished after the worker has drained
                # its queue, so any row written before that moment is
                # visible to the read below. Reading in the opposite order
                # could render a "finished" screen that misses the last
                # rows.
                info = read_runner_info(args.status_file)
                status = str(info.get("status", "running")).lower()
                finished = status in FINISHED_STATES

                try:
                    stat = session_csv.stat()
                    signature = (stat.st_mtime_ns, stat.st_size)
                except OSError:
                    signature = None

                read_ok = True

                if signature != csv_signature:
                    fresh = read_session_rows(session_csv)

                    if fresh is None:
                        read_ok = False  # keep old rows, retry next poll
                    else:
                        rows, csv_signature = fresh, signature

                # Rewrite the TXT report only when something changed.
                current = (csv_signature, status)

                if read_ok and current != report_signature:
                    try:
                        write_report(
                            args.output,
                            render_report(
                                rows,
                                args,
                                session_start,
                                info,
                                datetime.now(timezone.utc),
                            ),
                        )
                        report_signature = current
                    except OSError:
                        pass  # e.g. file open in an editor; retry later

                if finished and read_ok:
                    break

                # Plain output has no live screen: print the Pinnacle data
                # quality once, as soon as the runner has published it. (The
                # final report below is the complete, unchanged summary.)
                if not interactive and not quality_printed:
                    early = quality_section(
                        pinnacle_info(info),
                        title=(
                            "PINNACLE DATA QUALITY "
                            "(known before Betclic collection)"
                        ),
                    )

                    if early:
                        print("\n".join(early), flush=True)
                        quality_printed = True

                if not interactive and len(rows) != last_plain_count:
                    last_plain_count = len(rows)
                    print(
                        f"[{datetime.now():%H:%M:%S}] "
                        f"{len(rows)} observations processed "
                        f"(session {status})",
                        flush=True,
                    )

            if interactive:
                size = shutil.get_terminal_size(fallback=(120, 30))

                if (size.columns, size.lines) != last_size:
                    dirty = True

                if dirty:
                    lines, view.offset, view.body_h, view.max_offset = (
                        live_frame(
                            rows,
                            args,
                            session_start,
                            info,
                            datetime.now(timezone.utc),
                            size.lines,
                            size.columns,
                            view.offset,
                        )
                    )

                    draw(
                        lines,
                        size.columns,
                        clear=(size.columns, size.lines) != last_size,
                    )

                    last_size = (size.columns, size.lines)
                    dirty = False

            wait = max(0.0, next_refresh - time.monotonic())

            if keys is not None:
                pressed = keys.poll(min(wait, KEY_POLL_SECONDS))

                if pressed and apply_keys(view, pressed):
                    dirty = True
            else:
                time.sleep(min(wait, KEY_POLL_SECONDS))

    except KeyboardInterrupt:
        print("\nMonitor interrupted.", flush=True)

    finally:
        if keys is not None:
            keys.stop()

        if interactive:
            sys.stdout.write(ALT_SCREEN_OFF)
            sys.stdout.flush()

    # Leave the COMPLETE table in the normal terminal buffer so that it can
    # be scrolled with the terminal's own scrollbar / mouse wheel.
    print(
        "\n".join(
            static_lines(
                rows,
                args,
                session_start,
                info,
                datetime.now(timezone.utc),
                compact=True,
            )
        ),
        flush=True,
    )

    if str(info.get("status", "")).lower() in FINISHED_STATES:
        print("\nSession finished.", flush=True)
        print(f"Complete report saved to:\n{args.output}", flush=True)
        print(f"Session CSV:\n{session_csv}", flush=True)

    # The runner launches this monitor in a persistent terminal.
    try:
        input("\nPress Enter to close this monitor window...")
    except EOFError:
        pass


if __name__ == "__main__":
    main()