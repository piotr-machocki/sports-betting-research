"""
Per-run result files: raw data for analysis and display reports.

For every session two kinds of output are written under data/results/:

    raw/<session_id>.csv       one row per observation, every calculated value
    raw/<session_id>.json      the same records (typed) plus session metadata
    reports/<session_id>.txt   the final terminal table (all matches, all EV
                               columns), same layout as the live monitor
    reports/latest.txt         copy of the newest report

The session working files in data/results/sessions/ (CSV, status JSON and the
full TXT report with observation details) are left untouched.

This module also owns the names of the per-method columns in the session CSV,
so the worker, the monitor and the exports agree on them.

Usage (backfill or re-export existing sessions):
    python scripts/results_store.py --all
    python scripts/results_store.py --session-id session_20261030_194220_1ac950
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from betting.margin import METHODS  # noqa: E402
from betting.value import ev_by_method  # noqa: E402

RESULTS_DIR = ROOT / "data" / "results"
SESSIONS_DIR = RESULTS_DIR / "sessions"
RAW_DIR = RESULTS_DIR / "raw"
REPORTS_DIR = RESULTS_DIR / "reports"

# Ranking, best side and statistics use the reference method. Its values are
# stored in the session CSV without a suffix (probability1, ev1, best_ev).
REFERENCE_METHOD = "proportional"
ALT_METHODS = [method for method in METHODS if method != REFERENCE_METHOD]

SIDES = (1, 2)


# ------------------------------------------------- session CSV columns ------


def probability_column(method: str, side: int) -> str:
    """Session CSV column holding the fair probability of a side."""
    if method == REFERENCE_METHOD:
        return f"probability{side}"

    return f"probability{side}_{method}"


def ev_column(method: str, side: int) -> str:
    """Session CSV column holding the EV of a side."""
    if method == REFERENCE_METHOD:
        return f"ev{side}"

    return f"ev{side}_{method}"


# Columns appended to the session CSV for the non-reference methods.
METHOD_FIELDS = [
    column
    for method in ALT_METHODS
    for column in (
        *(probability_column(method, side) for side in SIDES),
        *(ev_column(method, side) for side in SIDES),
    )
]


def method_columns(
    results: dict[str, tuple[list[float], list[float]]],
) -> dict[str, float]:
    """Session CSV cells for every method from betting.value.ev_by_method."""
    cells = {}

    for method, (probabilities, evs) in results.items():
        for side, probability, ev in zip(SIDES, probabilities, evs):
            cells[probability_column(method, side)] = round(probability, 6)
            cells[ev_column(method, side)] = round(ev, 6)

    return cells


def _number(value: Any) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None

        return float(value)

    except (TypeError, ValueError):
        return None


def complete_methods(row: dict) -> dict:
    """Fill missing per-method columns of a row that has a reference EV.

    Sessions processed before the per-method columns existed only stored the
    proportional values; the other methods are recalculated from the stored
    odds with the same function the worker uses. Rows without a reference EV
    (not found, live, invalid odds) are returned unchanged.
    """
    if _number(row.get("ev1")) is None or all(
        _number(row.get(column)) is not None for column in METHOD_FIELDS
    ):
        return row

    odds = [
        _number(row.get(key))
        for key in (
            "pinnacle_odds1",
            "pinnacle_odds2",
            "betclic_odds_for_pin_p1",
            "betclic_odds_for_pin_p2",
        )
    ]

    if any(value is None for value in odds):
        return row

    try:
        results = ev_by_method(odds[:2], odds[2:])
    except (ValueError, ArithmeticError):
        return row

    cells = method_columns(
        {method: results[method] for method in ALT_METHODS}
    )

    return {**row, **{key: str(value) for key, value in cells.items()}}


# --------------------------------------------------------- raw records -------

RAW_FIELDS = [
    "session_id",
    "observation_id",
    "match_no",
    "latest_for_match",
    "pinnacle_tournament",
    "pinnacle_player1",
    "pinnacle_player2",
    "betclic_player1",
    "betclic_player2",
    "status",
    "confidence",
    "betclic_status",
    "pinnacle_captured_at",
    "betclic_captured_at",
    "observation_gap_seconds",
    "pinnacle_odds1",
    "pinnacle_odds2",
    "pinnacle_margin",
    "betclic_odds1",
    "betclic_odds2",
    "betclic_margin",
    *(
        f"{kind}{side}_{method}"
        for method in METHODS
        for kind in ("probability", "ev")
        for side in SIDES
    ),
    "best_side",
    "best_player",
    "best_ev",
    "url",
    "note",
]

NUMERIC_FIELDS = {
    "match_no",
    "observation_gap_seconds",
    "pinnacle_odds1",
    "pinnacle_odds2",
    "pinnacle_margin",
    "betclic_odds1",
    "betclic_odds2",
    "betclic_margin",
    "best_side",
    "best_ev",
    *(
        f"{kind}{side}_{method}"
        for method in METHODS
        for kind in ("probability", "ev")
        for side in SIDES
    ),
}


def overround(odds1: Any, odds2: Any) -> float | None:
    """Bookmaker margin of a two-way market: sum of 1/odds minus 1."""
    first, second = _number(odds1), _number(odds2)

    if first is None or second is None or first <= 1 or second <= 1:
        return None

    return round(1 / first + 1 / second - 1, 6)


def raw_record(row: dict, session_id: str, latest: bool) -> dict:
    """One analysis-ready record (CSV cell values) from a session CSV row.

    Betclic odds are in Pinnacle player order; side 1/2 always means
    pinnacle_player1/2. best_side and best_ev follow the reference method,
    exactly like the live table.
    """
    ev1 = _number(row.get("ev1"))
    ev2 = _number(row.get("ev2"))

    record = {
        "session_id": session_id,
        "observation_id": row.get("observation_id", ""),
        "match_no": row.get("match_no", ""),
        "latest_for_match": latest,
        "pinnacle_tournament": row.get("pinnacle_tournament", ""),
        "pinnacle_player1": row.get("pinnacle_player1", ""),
        "pinnacle_player2": row.get("pinnacle_player2", ""),
        "betclic_player1": row.get("betclic_player1", ""),
        "betclic_player2": row.get("betclic_player2", ""),
        "status": row.get("status", ""),
        "confidence": row.get("confidence", ""),
        "betclic_status": row.get("betclic_status", ""),
        "pinnacle_captured_at": row.get("pinnacle_captured_at", ""),
        "betclic_captured_at": row.get("betclic_captured_at", ""),
        "observation_gap_seconds": row.get("observation_gap_seconds", ""),
        "pinnacle_odds1": row.get("pinnacle_odds1", ""),
        "pinnacle_odds2": row.get("pinnacle_odds2", ""),
        "pinnacle_margin": overround(
            row.get("pinnacle_odds1"), row.get("pinnacle_odds2")
        ),
        "betclic_odds1": row.get("betclic_odds_for_pin_p1", ""),
        "betclic_odds2": row.get("betclic_odds_for_pin_p2", ""),
        "betclic_margin": overround(
            row.get("betclic_odds_for_pin_p1"),
            row.get("betclic_odds_for_pin_p2"),
        ),
        "best_side": (
            None if ev1 is None or ev2 is None else (1 if ev1 >= ev2 else 2)
        ),
        "best_player": row.get("best_player", ""),
        "best_ev": row.get("best_ev", ""),
        "url": row.get("url", ""),
        "note": row.get("note", ""),
    }

    for method in METHODS:
        for side in SIDES:
            record[f"probability{side}_{method}"] = row.get(
                probability_column(method, side), ""
            )
            record[f"ev{side}_{method}"] = row.get(
                ev_column(method, side), ""
            )

    return {
        key: "" if record[key] is None else record[key]
        for key in RAW_FIELDS
    }


def typed_record(record: dict) -> dict:
    """JSON version of a raw record: numbers as numbers, blanks as null."""
    typed = {}

    for key, value in record.items():
        if key in NUMERIC_FIELDS:
            parsed = _number(value)
            typed[key] = (
                int(parsed)
                if parsed is not None and key in ("match_no", "best_side")
                else parsed
            )
        elif value == "":
            typed[key] = None
        else:
            typed[key] = value

    return typed


def raw_records(rows: list[dict], session_id: str) -> list[dict]:
    """Every observation in session CSV order."""
    import live_monitor

    latest_ids = {
        id(row) for row in live_monitor.latest_per_match(rows)
    }

    return [
        raw_record(row, session_id, id(row) in latest_ids)
        for row in rows
    ]


# --------------------------------------------------------------- writing ----


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(text, encoding="utf-8", newline="")
    temp_path.replace(path)


def write_raw(
    records: list[dict],
    session_id: str,
    info: dict,
    raw_dir: Path,
    exported_at: datetime,
) -> tuple[Path, Path]:
    """Write <session_id>.csv and <session_id>.json; return both paths."""
    csv_path = raw_dir / f"{session_id}.csv"
    json_path = raw_dir / f"{session_id}.json"

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=RAW_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(records)

    _write_text_atomic(csv_path, buffer.getvalue())

    document = {
        "session_id": session_id,
        "exported_at": exported_at.isoformat(timespec="seconds"),
        "session_status": info.get("status"),
        "session_start": info.get("session_start"),
        "session_finished_at": info.get("session_finished_at"),
        "reference_method": REFERENCE_METHOD,
        "methods": list(METHODS),
        "fields": RAW_FIELDS,
        "observations": [typed_record(record) for record in records],
    }

    _write_text_atomic(
        json_path,
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
    )

    return csv_path, json_path


def export_session(
    session_id: str,
    report_args: argparse.Namespace,
    *,
    rows: list[dict] | None = None,
    info: dict | None = None,
    sessions_dir: Path = SESSIONS_DIR,
    raw_dir: Path = RAW_DIR,
    reports_dir: Path = REPORTS_DIR,
    update_latest: bool = True,
    now: datetime | None = None,
) -> dict[str, Path]:
    """Write the raw data and the display report of one session.

    Rows and status default to the session's files in `sessions_dir`.
    Returns the written paths keyed by kind.
    """
    import live_monitor

    now = now or datetime.now(timezone.utc)

    if rows is None:
        rows = live_monitor.read_session_rows(
            sessions_dir / f"{session_id}.csv"
        ) or []

    if info is None:
        info = live_monitor.read_runner_info(
            sessions_dir / f"{session_id}_status.json"
        )

    rows = [complete_methods(row) for row in rows]

    session_start = (
        live_monitor.parse_timestamp(info.get("session_start"))
        or live_monitor.parse_timestamp(
            min((row.get("pinnacle_captured_at") or "" for row in rows), default="")
        )
        or now
    )

    csv_path, json_path = write_raw(
        raw_records(rows, session_id), session_id, info, raw_dir, now
    )

    report = live_monitor.render_display_report(
        rows, report_args, session_start, info, now
    )

    report_path = reports_dir / f"{session_id}.txt"
    _write_text_atomic(report_path, report)

    written = {"raw_csv": csv_path, "raw_json": json_path, "report": report_path}

    if update_latest:
        latest_path = reports_dir / "latest.txt"
        _write_text_atomic(latest_path, report)
        written["latest_report"] = latest_path

    return written


# ------------------------------------------------------------------- CLI ----


def report_namespace(
    session_id: str,
    min_ev: float = 0.02,
    max_age: float = 15.0,
) -> argparse.Namespace:
    """The monitor settings a report is rendered with."""
    return argparse.Namespace(
        session_id=session_id,
        expected=None,
        min_ev=min_ev,
        max_age=max_age,
        top=None,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write raw data and display reports for sessions."
    )

    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--session-id",
        action="append",
        help="Session to export (repeatable).",
    )
    target.add_argument(
        "--all",
        action="store_true",
        help="Export every session in data/results/sessions/.",
    )

    parser.add_argument("--min-ev", type=float, default=0.02)
    parser.add_argument("--max-age", type=float, default=15.0)

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    session_ids = (
        sorted(path.stem for path in SESSIONS_DIR.glob("session_*.csv"))
        if args.all
        else args.session_id
    )

    if not session_ids:
        raise SystemExit(f"No sessions found in {SESSIONS_DIR}")

    for index, session_id in enumerate(session_ids):
        if not (SESSIONS_DIR / f"{session_id}.csv").exists():
            print(f"[SKIPPED] {session_id}: no session CSV")
            continue

        written = export_session(
            session_id,
            report_namespace(session_id, args.min_ev, args.max_age),
            # Session IDs sort chronologically: latest = newest.
            update_latest=index == len(session_ids) - 1,
        )

        print(f"[EXPORTED] {session_id}")

        for path in written.values():
            print(f"    {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
