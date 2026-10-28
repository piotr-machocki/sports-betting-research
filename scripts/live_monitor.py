"""
Live terminal monitor for the Betclic/Pinnacle research pipeline.

Reads only the current session's processed CSV from:
    data/results/sessions/<session_id>.csv

Continuously displays observations sorted by best EV and saves
a detailed session report to a TXT file.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

SESSIONS_DIR = ROOT / "data" / "results" / "sessions"

POLL_SECONDS = 1.0

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
        help="Maximum observations displayed in the terminal.",
    )

    parser.add_argument(
        "--min-ev",
        type=float,
        default=0.02,
        help="Threshold used to count positive-EV observations.",
    )

    return parser.parse_args()


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


def read_session_rows(path: Path) -> list[dict[str, str]]:
    """
    Read only this session's processed CSV.

    A temporary read error can occur if the worker is writing the
    file simultaneously. In that case, retry on the next poll.
    """

    if not path.exists():
        return []

    try:
        with path.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as file:
            return list(csv.DictReader(file))

    except (OSError, csv.Error):
        return []


def sort_key(row: dict) -> tuple:
    ev = number(row.get("best_ev"))

    if ev is None:
        return (1, 0.0)

    return (0, -ev)


def format_probability(value: Any) -> str:
    parsed = number(value)

    if parsed is None:
        return "--"

    return f"{parsed:.1%}"


def format_ev(value: Any) -> str:
    parsed = number(value)

    if parsed is None:
        return "--"

    return f"{parsed:+.2%}"


def format_odds(value: Any) -> str:
    parsed = number(value)

    if parsed is None:
        return "--"

    return f"{parsed:.3f}".rstrip("0").rstrip(".")


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


def table_lines(rows: list[dict]) -> list[str]:
    headers = (
        f"{'#':>3}  "
        f"{'MATCH':<36} "
        f"{'P1 P':>7} "
        f"{'PIN 1':>7} "
        f"{'BET 1':>7} "
        f"{'EV 1':>8} "
        f"{'P2 P':>7} "
        f"{'PIN 2':>7} "
        f"{'BET 2':>7} "
        f"{'EV 2':>8} "
        f"{'BEST EV':>9} "
        f"{'STATUS':<12}"
    )

    lines = [headers, "-" * len(headers)]

    for index, row in enumerate(rows, start=1):
        lines.append(
            f"{index:>3}  "
            f"{shorten(match_name(row), 36):<36} "
            f"{format_probability(row.get('probability1')):>7} "
            f"{format_odds(row.get('pinnacle_odds1')):>7} "
            f"{format_odds(row.get('betclic_odds_for_pin_p1')):>7} "
            f"{format_ev(row.get('ev1')):>8} "
            f"{format_probability(row.get('probability2')):>7} "
            f"{format_odds(row.get('pinnacle_odds2')):>7} "
            f"{format_odds(row.get('betclic_odds_for_pin_p2')):>7} "
            f"{format_ev(row.get('ev2')):>8} "
            f"{format_ev(row.get('best_ev')):>9} "
            f"{shorten(row.get('status', ''), 12):<12}"
        )

    return lines


def session_statistics(
    rows: list[dict],
    min_ev: float,
) -> list[str]:
    found = sum(
        row.get("status") == "FOUND"
        for row in rows
    )

    not_found = sum(
        row.get("status") == "NOT FOUND"
        for row in rows
    )

    errors = sum(
        row.get("status") == "ERROR"
        for row in rows
    )

    calculated = sum(
        number(row.get("best_ev")) is not None
        for row in rows
    )

    positive = sum(
        (ev := number(row.get("best_ev"))) is not None
        and ev >= min_ev
        for row in rows
    )

    return [
        "",
        "=" * 100,
        "SESSION STATISTICS",
        "=" * 100,
        f"Observations processed: {len(rows)}",
        f"Matches found:          {found}",
        f"Matches not found:      {not_found}",
        f"Errors:                 {errors}",
        f"EV calculated:          {calculated}",
        f"Best EV >= {min_ev:.2%}:     {positive}",
        "=" * 100,
    ]


def render_report(
    rows: list[dict],
    args: argparse.Namespace,
    session_start: datetime,
    finished: bool,
    runner_status: str,
) -> str:
    sorted_rows = sorted(rows, key=sort_key)

    now = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )

    lines = [
        "=" * 100,
        "SPORTS BETTING RESEARCH — LIVE SESSION",
        "=" * 100,
        f"Session ID:      {args.session_id}",
        f"Session started: {session_start.isoformat(timespec='seconds')}",
        f"Last updated:    {now}",
        f"Status:          {runner_status.upper()}",
        f"Minimum EV:      {args.min_ev:.2%}",
        "",
        "EV is calculated from probabilities derived using proportional",
        "devigging of Pinnacle odds. P1/P2 follow Pinnacle player order.",
        "Betclic odds are mapped to that same player order.",
        "",
        "=" * 100,
        "MATCHES SORTED BY BEST EV (highest first)",
        "=" * 100,
    ]

    displayed_rows = sorted_rows[: args.top]
    lines.extend(table_lines(displayed_rows))

    if len(sorted_rows) > args.top:
        lines.extend([
            "",
            f"Terminal display limited to {args.top} rows.",
            f"Total observations in this session: {len(sorted_rows)}.",
            "The TXT report contains details for every observation.",
        ])

    lines.extend(session_statistics(sorted_rows, args.min_ev))

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


def read_runner_status(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("status", "running")).lower()

    except (OSError, json.JSONDecodeError, AttributeError):
        return "running"


def write_report(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def clear_terminal() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def main() -> None:
    args = parse_args()

    if args.top < 1:
        raise SystemExit("--top must be at least 1")

    if not 0 <= args.min_ev:
        raise SystemExit("--min-ev must be non-negative")

    session_start = parse_timestamp(args.session_start)

    if session_start is None:
        raise SystemExit("Invalid --session-start timestamp")

    session_csv = SESSIONS_DIR / f"{args.session_id}.csv"

    args.output = args.output.resolve()
    args.status_file = args.status_file.resolve()

    print("Live monitor started.", flush=True)
    print(f"Session ID: {args.session_id}", flush=True)
    print(f"Session data: {session_csv}", flush=True)
    print(f"Session report: {args.output}", flush=True)

    try:
        while True:
            rows = read_session_rows(session_csv)
            rows.sort(key=sort_key)

            runner_status = read_runner_status(args.status_file)
            finished = runner_status in {
                "finished",
                "failed",
                "interrupted",
            }

            report = render_report(
                rows=rows,
                args=args,
                session_start=session_start,
                finished=finished,
                runner_status=runner_status,
            )

            # Save the complete report, including every observation.
            write_report(args.output, report)

            # Show only the summary and top observations in the terminal.
            clear_terminal()
            summary = report.split("OBSERVATION DETAILS")[0]
            print(summary, flush=True)

            if finished:
                print("\nSession finished.", flush=True)
                print(
                    f"Complete report saved to:\n{args.output}",
                    flush=True,
                )
                print(
                    f"Session CSV:\n{session_csv}",
                    flush=True,
                )
                break

            time.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        print("\nMonitor interrupted.", flush=True)

    # The runner launches this monitor in a persistent terminal.
    try:
        input("\nPress Enter to close this monitor window...")
    except EOFError:
        pass


if __name__ == "__main__":
    main()
