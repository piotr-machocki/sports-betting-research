"""
Live terminal monitor for the Betclic/Pinnacle research pipeline.

Reads only the current session's processed CSV from:
    data/results/sessions/<session_id>.csv

Redraws a colour table in place (same colour conventions as compare_odds.py),
one row per match (latest observation), sorted by best EV, and saves a
plain-text session report with every observation.

The CSV is re-read only when it changes; the screen is redrawn every second
so the clocks (elapsed time, data age) keep ticking.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

SESSIONS_DIR = ROOT / "data" / "results" / "sessions"

POLL_SECONDS = 1.0
FINISHED_STATES = {"finished", "failed", "interrupted"}

# Warn when a running session has produced nothing for this long.
STUCK_AFTER_SECONDS = 90

PW = 22  # player-name column width

FIELDS = [
    "observation_id",
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
        default=20,
        help="Maximum matches displayed in the terminal.",
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
            "an observation is flagged STALE. Default: 15."
        ),
    )

    parser.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="auto",
        help="ANSI colours in the terminal table.",
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

    return [
        row
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


def sort_key(row: dict) -> tuple:
    ev = number(row.get("best_ev"))

    if ev is None:
        return (1, 0.0)

    return (0, -ev)


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


def shorten(value: Any, width: int) -> str:
    text = str(value or "")

    if len(text) <= width:
        return text

    return text[: width - 3] + "..."


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


def latest_per_match(rows: list[dict]) -> list[dict]:
    """One row per match: the most recent Betclic observation.

    Normally the scraper emits one observation per match and this is a no-op;
    if a match is observed twice, only the newest one is shown.
    """
    newest: dict[tuple, tuple[float, dict]] = {}

    for row in rows:
        key = (
            row.get("pinnacle_player1", ""),
            row.get("pinnacle_player2", ""),
            row.get("pinnacle_tournament", ""),
        )

        stamp = parse_timestamp(row.get("betclic_captured_at"))
        value = stamp.timestamp() if stamp else float("-inf")

        if key not in newest or value >= newest[key][0]:
            newest[key] = (value, row)

    return [row for _, row in newest.values()]


def is_stale(row: dict, max_age_minutes: float) -> bool:
    gap = number(row.get("observation_gap_seconds"))
    return gap is not None and gap > max_age_minutes * 60


def flags_for(row: dict, args: argparse.Namespace) -> str:
    flags = []

    if best_side(row) is None:
        status = str(row.get("status") or "?")

        if status == "ERROR":
            flags.append(paint(status, "1", "31"))
        elif status != "FOUND":
            flags.append(paint(status, "1", "33"))
        elif str(row.get("betclic_status", "")).lower() == "live":
            flags.append(paint("LIVE", "1", "33"))
        else:
            flags.append(paint("NO EV", "90"))

    if is_stale(row, args.max_age):
        flags.append(paint("STALE", "1", "33"))

    return "".join(" " + flag for flag in flags)


# ---------------------------------------------------------------- table -----


def table_lines(rows: list[dict], args: argparse.Namespace) -> list[str]:
    header = (
        f"{'#':>3}  {'BET ON':<{PW}} {'Betclic':>7} {'Pinnacle':>8} "
        f"{'P(ref)':>7} {'EV(ref)':>8} {'GAP':>11}  {'CONF':<8} "
        f"{'OPPONENT':<{PW + 3}}"
    )

    lines = [paint(header, "1"), "-" * len(header)]

    if not rows:
        lines.append(paint("  waiting for Betclic observations...", "2"))
        return lines

    for index, row in enumerate(rows, start=1):
        side = best_side(row)

        if side is None:
            name = row.get("pinnacle_player1") or "?"
            opponent = row.get("pinnacle_player2") or "?"

            player_cell = paint(f"{name[:PW]:<{PW}}", "2")
            betclic = probability = f"{'--':>7}"
            pinnacle = f"{'--':>8}"
            ev_cell = paint(f"{'--':>8}", "2")

        else:
            other = 2 if side == 1 else 1

            name = row.get(f"pinnacle_player{side}") or "?"
            opponent = row.get(f"pinnacle_player{other}") or "?"
            ev = number(row.get(f"ev{side}"))

            player_cell = paint(f"{name[:PW]:<{PW}}", "1", "36")
            betclic = format_odds(
                row.get(f"betclic_odds_for_pin_p{side}")
            ).rjust(7)
            pinnacle = format_odds(
                row.get(f"pinnacle_odds{side}")
            ).rjust(8)
            probability = format_probability(
                row.get(f"probability{side}")
            ).rjust(7)
            ev_cell = paint(
                f"{ev * 100:>+7.2f}%",
                *ev_style(ev, args.min_ev),
            )

        gap = number(row.get("observation_gap_seconds"))
        gap_text = fmt_age(gap) if gap is not None else "--"
        gap_cell = paint(
            f"{gap_text:>11}",
            *(("33",) if is_stale(row, args.max_age) else ("2",)),
        )

        confidence = f"{shorten(row.get('confidence'), 8):<8}"

        lines.append(
            f"{index:>3}  {player_cell} {betclic} {pinnacle} "
            f"{probability} {ev_cell} {gap_cell}  {confidence} "
            f"{paint(f'vs {opponent[:PW]:<{PW}}', '2')}"
            f"{flags_for(row, args)}"
        )

    return lines


# ------------------------------------------------------------ statistics ----


def stats_box(
    all_rows: list[dict],
    latest: list[dict],
    args: argparse.Namespace,
) -> list[str]:
    def count(predicate) -> int:
        return sum(1 for row in latest if predicate(row))

    evs = [number(row.get("best_ev")) for row in latest]

    processed = (
        f"{len(all_rows)} / {args.expected}"
        if args.expected
        else str(len(all_rows))
    )

    items = [
        ("Observations processed", processed),
        ("Unique matches", str(len(latest))),
        ("Found", str(count(lambda r: r.get("status") == "FOUND"))),
        ("Not found", str(count(lambda r: r.get("status") == "NOT FOUND"))),
        ("Errors", str(count(lambda r: r.get("status") == "ERROR"))),
        (
            "FOUND but no EV (live etc.)",
            str(
                count(
                    lambda r: r.get("status") == "FOUND"
                    and number(r.get("best_ev")) is None
                )
            ),
        ),
        ("EV calculated", str(sum(ev is not None for ev in evs))),
        ("EV(ref) > 0%", str(sum(ev is not None and ev > 0 for ev in evs))),
        (
            f"EV(ref) >= {args.min_ev:.2%}",
            str(sum(ev is not None and ev >= args.min_ev for ev in evs)),
        ),
        (
            f"Stale (gap > {args.max_age:g} min)",
            str(count(lambda r: is_stale(r, args.max_age))),
        ),
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

    # A finished session is measured up to its end, not up to "now".
    end = parse_timestamp(info.get("session_finished_at")) if finished else None
    clock = end or now

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
        (
            f"Session: {args.session_id}   "
            f"Started: {session_start:%H:%M:%S} UTC   "
            f"Elapsed: {fmt_age(elapsed)}"
        ),
        (
            f"Pinnacle data age: {fmt_age(pinnacle_age)}   "
            f"Last observation: {last_text}   "
            f"Max gap: {fmt_age(max(gaps)) if gaps else '--'}"
        ),
    ]

    waiting_for_more = not args.expected or processed < args.expected

    if not finished and waiting_for_more and idle > STUCK_AFTER_SECONDS:
        lines[2] += "   " + paint(
            f"NO NEW OBSERVATION FOR {fmt_age(idle).upper()} - "
            "scraper may be stuck",
            "1",
            "31",
        )

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
    else:
        lines.append(f"Observations: {processed}")

    return lines


def legend_lines(args: argparse.Namespace) -> list[str]:
    threshold = f"{args.min_ev * 100:g}%"

    return [
        "Legend: "
        + paint("BET ON", "1", "36")
        + " = side with the best EV | "
        + paint("red", "31") + " EV<0% | "
        + paint("gray", "90") + " EV=0% | "
        + paint("yellow", "33") + f" 0%<EV<{threshold} | "
        + paint("green", "1", "32") + f" EV>={threshold}",
        f"GAP = Betclic capture minus Pinnacle capture; "
        f"{paint('STALE', '1', '33')} = GAP > {args.max_age:g} min. "
        "CONF = scraper match confidence.",
        "EV(ref) = proportional devig of Pinnacle odds. One row per match "
        "(latest observation), best EV first.",
    ]


def screen_lines(
    all_rows: list[dict],
    args: argparse.Namespace,
    session_start: datetime,
    info: dict,
    now: datetime,
    term_lines: int | None,
) -> list[str]:
    latest = sorted(latest_per_match(all_rows), key=sort_key)

    pre = (
        header_lines(all_rows, args, session_start, info, now)
        + [""]
        + stats_box(all_rows, latest, args)
        + [""]
    )
    legend = legend_lines(args)
    post = [""] + legend

    top = args.top

    if term_lines:
        # Never draw more lines than the window holds, or the in-place
        # redraw would scroll. 2 = table header, 1 = "more rows" line.
        available = term_lines - 1 - len(pre) - 2 - 1 - len(post)

        if available < 5:
            # Short window: keep only the first legend line.
            post = legend[:1]
            available = term_lines - 1 - len(pre) - 2 - 1 - len(post)

        top = max(3, min(top, available))

    shown = latest[:top]
    lines = pre + table_lines(shown, args)

    if len(latest) > len(shown):
        lines.append(
            paint(
                f"... {len(latest) - len(shown)} more matches "
                "(full list in the TXT report)",
                "2",
            )
        )

    return lines + post


# ----------------------------------------------------------------- report ---


def render_report(
    all_rows: list[dict],
    args: argparse.Namespace,
    session_start: datetime,
    info: dict,
    now: datetime,
) -> str:
    with plain():
        lines = screen_lines(all_rows, args, session_start, info, now, None)

    lines.extend([
        "",
        "=" * 100,
        f"Report generated: {now.isoformat(timespec='seconds')}",
        "P1/P2 follow Pinnacle player order. Betclic odds are mapped to",
        "that same player order. The table above shows the latest",
        f"observation per match (top {args.top}); details below cover",
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
                (
                    f"EVs: {format_ev(row.get('ev1'))} / "
                    f"{format_ev(row.get('ev2'))}"
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


def write_report(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


# ------------------------------------------------------------------ screen --


def draw(lines: list[str], first: bool) -> None:
    if Style.enabled:
        # Cursor home + clear-to-end-of-line: redraws in place, no flicker.
        frame = (
            ("\033[2J" if first else "")
            + "\033[H"
            + "".join(line + "\033[K\n" for line in lines)
            + "\033[J"
        )
        sys.stdout.write(frame)
        sys.stdout.flush()
    else:
        os.system("cls" if os.name == "nt" else "clear")
        print("\n".join(lines), flush=True)


def main() -> None:
    args = parse_args()

    if args.top < 1:
        raise SystemExit("--top must be at least 1")

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

    session_csv = SESSIONS_DIR / f"{args.session_id}.csv"

    args.output = args.output.resolve()
    args.status_file = args.status_file.resolve()

    rows: list[dict] = []
    csv_signature = None
    report_signature = None
    first_frame = True
    info: dict = {}

    if Style.enabled:
        sys.stdout.write("\033[?25l")  # hide cursor while redrawing

    try:
        while True:
            # Read the runner status BEFORE the CSV. The runner only marks the
            # session finished after the worker has drained its queue, so any
            # row written before that moment is visible to the read below.
            # Reading in the opposite order could render a "finished" screen
            # that misses the last rows.
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
                    read_ok = False  # keep old rows, retry on the next poll
                else:
                    rows, csv_signature = fresh, signature

            now = datetime.now(timezone.utc)

            terminal_rows = shutil.get_terminal_size(fallback=(120, 30)).lines

            draw(
                screen_lines(
                    rows, args, session_start, info, now, terminal_rows
                ),
                first_frame,
            )
            first_frame = False

            # Rewrite the TXT report only when something actually changed.
            current = (csv_signature, status)

            if read_ok and current != report_signature:
                try:
                    write_report(
                        args.output,
                        render_report(rows, args, session_start, info, now),
                    )
                    report_signature = current
                except OSError:
                    pass  # e.g. file open in an editor; retry next poll

            if finished and read_ok:
                break

            time.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        print("\nMonitor interrupted.", flush=True)

    finally:
        if Style.enabled:
            sys.stdout.write("\033[?25h")  # restore cursor
            sys.stdout.flush()

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