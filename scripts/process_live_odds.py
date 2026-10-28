"""
Continuously process Betclic observations for one research session.

Session results:
    data/results/sessions/<session_id>.csv

Historical results:
    data/results/processed_odds.csv

Processed JSON messages:
    data/betclic/processed_queue/<session_id>/
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"

BETCLIC_DIR = ROOT / "data" / "betclic"
INCOMING_DIR = BETCLIC_DIR / "incoming"
PROCESSED_QUEUE_DIR = BETCLIC_DIR / "processed_queue"

RESULTS_DIR = ROOT / "data" / "results"
SESSIONS_DIR = RESULTS_DIR / "sessions"

POLL_SECONDS = 0.5

OUTPUT_FIELDS = [
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
        description="Process Betclic observations for one session."
    )

    parser.add_argument(
        "--session-id",
        required=True,
        help="Unique ID of the research session.",
    )

    parser.add_argument(
        "--stop-file",
        type=Path,
        default=BETCLIC_DIR / "STOP_PROCESSOR",
        help="Stop after the session queue has been drained.",
    )

    return parser.parse_args()


def load_function(filename: str, function_name: str):
    """Load an existing project function without assuming its module path."""

    candidates = list(SRC_DIR.rglob(filename))

    if not candidates:
        raise FileNotFoundError(
            f"Could not find {filename} under {SRC_DIR}"
        )

    if len(candidates) > 1:
        matches = [
            path
            for path in candidates
            if path.parent.name == "betting"
        ]

        if len(matches) == 1:
            candidates = matches

    path = candidates[0]

    module_name = f"live_processor_{path.stem}"

    spec = importlib.util.spec_from_file_location(
        module_name,
        path,
    )

    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    function = getattr(module, function_name, None)

    if function is None:
        raise AttributeError(
            f"{function_name} not found in {path}"
        )

    print(f"Using {function_name} from {path.relative_to(ROOT)}")

    return function


remove_margin = load_function("margin.py", "remove_margin")
calculate_ev = load_function("value.py", "calculate_ev")


def session_paths(session_id: str) -> dict[str, Path]:
    """Return the output paths belonging to this session."""

    return {
        "session_csv": SESSIONS_DIR / f"{session_id}.csv",
        "historical_csv": RESULTS_DIR / "processed_odds.csv",
        "archive_dir": PROCESSED_QUEUE_DIR / session_id,
    }


def as_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(parsed) or parsed <= 1:
        return None

    return parsed


def observation_gap_seconds(
    pinnacle_time: str,
    betclic_time: str,
) -> float | str:
    """Calculate the difference between two ISO timestamps."""

    if not pinnacle_time or not betclic_time:
        return ""

    try:
        p = datetime.fromisoformat(
            pinnacle_time.replace("Z", "+00:00")
        )

        b = datetime.fromisoformat(
            betclic_time.replace("Z", "+00:00")
        )

        return round((b - p).total_seconds(), 3)

    except (ValueError, TypeError):
        return ""


def calculate_observation(message: dict) -> dict:
    """Calculate fair probabilities and EV for a verified observation."""

    result = {field: "" for field in OUTPUT_FIELDS}

    result.update({
        "observation_id": message.get("observation_id", ""),
        "pinnacle_captured_at": message.get(
            "pinnacle_captured_at", ""
        ),
        "betclic_captured_at": message.get(
            "betclic_captured_at", ""
        ),
        "observation_gap_seconds": observation_gap_seconds(
            message.get("pinnacle_captured_at", ""),
            message.get("betclic_captured_at", ""),
        ),
        "pinnacle_player1": message.get("pinnacle_player1", ""),
        "pinnacle_player2": message.get("pinnacle_player2", ""),
        "pinnacle_tournament": message.get(
            "pinnacle_tournament", ""
        ),
        "pinnacle_odds1": message.get("pinnacle_odds1", ""),
        "pinnacle_odds2": message.get("pinnacle_odds2", ""),
        "betclic_player1": message.get("betclic_player1", ""),
        "betclic_player2": message.get("betclic_player2", ""),
        "betclic_odds_for_pin_p1": message.get(
            "betclic_odds_for_pin_p1", ""
        ),
        "betclic_odds_for_pin_p2": message.get(
            "betclic_odds_for_pin_p2", ""
        ),
        "status": message.get("status", ""),
        "confidence": message.get("confidence", ""),
        "betclic_status": message.get("betclic_status", ""),
        "url": message.get("url", ""),
        "note": message.get("note", ""),
    })

    if message.get("status") != "FOUND":
        result["note"] = (
            f"Not calculated: status is "
            f"{message.get('status', 'unknown')}. "
            f"{result['note']}"
        ).strip()

        return result

    if message.get("odds_ok") is not True:
        result["note"] = (
            f"Not calculated: odds_ok="
            f"{message.get('odds_ok')!r}. "
            f"{result['note']}"
        ).strip()

        return result

    if str(message.get("betclic_status", "")).lower() == "live":
        result["note"] = (
            "Not calculated: Betclic event is live."
        )

        return result

    pin1 = as_float(message.get("pinnacle_odds1"))
    pin2 = as_float(message.get("pinnacle_odds2"))

    bet1 = as_float(
        message.get("betclic_odds_for_pin_p1")
    )

    bet2 = as_float(
        message.get("betclic_odds_for_pin_p2")
    )

    if None in (pin1, pin2, bet1, bet2):
        result["note"] = (
            "Not calculated: invalid or missing odds. "
            f"{result['note']}"
        ).strip()

        return result

    try:
        probabilities = remove_margin(
            [pin1, pin2],
            method="proportional",
        )

        probability1, probability2 = probabilities

        ev1 = calculate_ev(probability1, bet1)
        ev2 = calculate_ev(probability2, bet2)

    except (ValueError, ArithmeticError) as exc:
        result["note"] = f"Calculation error: {exc}"
        return result

    best_ev = max(ev1, ev2)

    best_player = (
        message.get("pinnacle_player1", "")
        if ev1 >= ev2
        else message.get("pinnacle_player2", "")
    )

    result.update({
        "probability1": round(probability1, 6),
        "probability2": round(probability2, 6),
        "ev1": round(ev1, 6),
        "ev2": round(ev2, 6),
        "best_ev": round(best_ev, 6),
        "best_player": best_player,
    })

    return result


def read_processed_ids(path: Path) -> set[str]:
    """Load observation IDs already saved in a CSV."""

    if not path.exists():
        return set()

    with path.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        return {
            row["observation_id"]
            for row in csv.DictReader(file)
            if row.get("observation_id")
        }


def append_csv_row(path: Path, row: dict) -> None:
    """Append one row, creating the CSV and header when necessary."""

    path.parent.mkdir(parents=True, exist_ok=True)

    write_header = (
        not path.exists()
        or path.stat().st_size == 0
    )

    with path.open(
        "a",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=OUTPUT_FIELDS,
            extrasaction="ignore",
        )

        if write_header:
            writer.writeheader()

        writer.writerow(row)


def append_result(
    result: dict,
    session_csv: Path,
    historical_csv: Path,
    session_ids: set[str],
    historical_ids: set[str],
) -> None:
    """
    Save the session result and the shared historical record.

    The session CSV is the primary output. The historical CSV is
    supplementary and is deduplicated independently.
    """

    observation_id = str(result["observation_id"])

    if observation_id in session_ids:
        return

    append_csv_row(session_csv, result)
    session_ids.add(observation_id)

    if observation_id not in historical_ids:
        append_csv_row(historical_csv, result)
        historical_ids.add(observation_id)


def process_file(
    path: Path,
    session_id: str,
    archive_dir: Path,
    session_csv: Path,
    historical_csv: Path,
    session_ids: set[str],
    historical_ids: set[str],
) -> bool:
    """
    Process a single queue message.

    Returns True if the message was successfully processed and archived.
    Messages belonging to another session are left untouched.
    """

    try:
        message = json.loads(path.read_text(encoding="utf-8"))

        message_session_id = message.get("session_id")

        if message_session_id != session_id:
            return False

        observation_id = message.get("observation_id")

        if not observation_id:
            raise ValueError(
                "Queue message has no observation_id"
            )

        if observation_id not in session_ids:
            result = calculate_observation(message)

            append_result(
                result=result,
                session_csv=session_csv,
                historical_csv=historical_csv,
                session_ids=session_ids,
                historical_ids=historical_ids,
            )

            if result.get("best_ev") != "":
                print(
                    f"[PROCESSED] "
                    f"{result['pinnacle_player1']} vs "
                    f"{result['pinnacle_player2']} | "
                    f"best EV="
                    f"{float(result['best_ev']):+.2%}",
                    flush=True,
                )
            else:
                print(
                    f"[SKIPPED] "
                    f"{result['pinnacle_player1']} vs "
                    f"{result['pinnacle_player2']} | "
                    f"{result['note']}",
                    flush=True,
                )

        archive_dir.mkdir(parents=True, exist_ok=True)

        archive_path = archive_dir / path.name

        if archive_path.exists():
            archive_path = archive_dir / (
                f"{path.stem}_{time.time_ns()}{path.suffix}"
            )

        path.replace(archive_path)

        return True

    except (
        OSError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
    ) as exc:
        print(
            f"[ERROR] {path.name}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )

        return False


def main() -> None:
    args = parse_args()

    if not args.session_id or any(
        character not in
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for character in args.session_id
    ):
        raise SystemExit(
            "--session-id may contain only letters, numbers, "
            "underscores, and hyphens."
        )

    args.stop_file = args.stop_file.resolve()

    paths = session_paths(args.session_id)

    session_csv = paths["session_csv"]
    historical_csv = paths["historical_csv"]
    archive_dir = paths["archive_dir"]

    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    session_ids = read_processed_ids(session_csv)
    historical_ids = read_processed_ids(historical_csv)

    print("Session odds processor started.", flush=True)
    print(f"Session ID: {args.session_id}", flush=True)
    print(f"Watching: {INCOMING_DIR}", flush=True)
    print(f"Session output: {session_csv}", flush=True)
    print(f"Historical output: {historical_csv}", flush=True)
    print(f"Archive: {archive_dir}", flush=True)
    print("Waiting for Betclic observations...\n", flush=True)

    try:
        while True:
            # Each observation is written as a separate JSON file.
            files = sorted(INCOMING_DIR.glob("*.json"))

            for path in files:
                process_file(
                    path=path,
                    session_id=args.session_id,
                    archive_dir=archive_dir,
                    session_csv=session_csv,
                    historical_csv=historical_csv,
                    session_ids=session_ids,
                    historical_ids=historical_ids,
                )

            # The runner signals completion after the scraper exits.
            # Drain this session's queue before stopping.
            if args.stop_file.exists():
                remaining = False

                for path in INCOMING_DIR.glob("*.json"):
                    try:
                        message = json.loads(
                            path.read_text(encoding="utf-8")
                        )

                        if message.get("session_id") == args.session_id:
                            remaining = True
                            break

                    except (OSError, json.JSONDecodeError):
                        # Don't stop while a message may still be written.
                        remaining = True
                        break

                if not remaining:
                    print(
                        "\nSession queue drained. "
                        "Processor stopping cleanly.",
                        flush=True,
                    )
                    break

            time.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        print(
            "\nProcessor interrupted by keyboard.",
            flush=True,
        )


if __name__ == "__main__":
    main()
