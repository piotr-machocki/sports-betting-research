"""
Run one Pinnacle -> Betclic -> live EV research session.

Workflow:
    1. Collect Pinnacle odds.
    2. Generate a unique session ID.
    3. Preserve a timestamped Pinnacle snapshot.
    4. Start the EV worker and live monitor.
    5. Collect Betclic observations into a unique raw CSV.
    6. Process observations for this session.
    7. Finish the session and save its status.

Usage:
    python scripts/run_once.py
    python scripts/run_once.py --limit 20
    python scripts/run_once.py --limit 20 --min-ev 0.02 --top 20
    python scripts/run_once.py --limit 20 --include-live
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid

from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------- paths -----

ROOT = Path(__file__).resolve().parents[1]

SRC_DIR = ROOT / "src"
SCRIPTS_DIR = ROOT / "scripts"

PINNACLE_SCRIPT = SRC_DIR / "betting" / "books" / "pinnacle.py"
BETCLIC_SCRIPT = SRC_DIR / "betting" / "books" / "betclic.py"

WORKER_SCRIPT = SCRIPTS_DIR / "process_live_odds.py"
MONITOR_SCRIPT = SCRIPTS_DIR / "live_monitor.py"

DATA_DIR = ROOT / "data"

PINNACLE_DIR = DATA_DIR / "pinnacle"
BETCLIC_DIR = DATA_DIR / "betclic"

PINNACLE_RAW_DIR = PINNACLE_DIR / "raw"
BETCLIC_RAW_DIR = BETCLIC_DIR / "raw"
INCOMING_DIR = BETCLIC_DIR / "incoming"

RESULTS_DIR = DATA_DIR / "results"
SESSIONS_DIR = RESULTS_DIR / "sessions"

STOP_FILE = BETCLIC_DIR / "STOP_PROCESSOR"


# ---------------------------------------------------------------- imports ---


sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

from betting.books import pinnacle  # noqa: E402
from perf_summary import performance_lines  # noqa: E402


# --------------------------------------------------------------- timestamps -


def utc_now() -> datetime:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc)


def timestamp_string(value: datetime) -> str:
    """Return an ISO-8601 UTC timestamp."""
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_session_id() -> str:
    """Create a unique ID shared by every file from this run."""
    timestamp = utc_now().strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:6]

    return f"session_{timestamp}_{suffix}"


def write_json_atomic(path: Path, data: dict) -> None:
    """Write JSON atomically to avoid partially written status files."""
    path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = path.with_name(f".{path.name}.tmp")

    temp_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    temp_path.replace(path)


# ---------------------------------------------------------------- arguments -


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect odds and launch the live EV monitor."
    )

    parser.add_argument(
        "--include-live",
        action="store_true",
        help="Include live matches if supported by the Betclic scraper.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Number of Pinnacle matches to test. Default: 5.",
    )

    parser.add_argument(
        "--min-ev",
        type=float,
        default=0.02,
        help="Threshold for the monitor's positive-EV count. Default: 2%%.",
    )

    parser.add_argument(
        "--top",
        type=int,
        default=20,
        help="Number of rows displayed in the live monitor. Default: 20.",
    )

    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run the Betclic browser without displaying it.",
    )

    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Skip the first N Pinnacle matches.",
    )

    return parser.parse_args()


# --------------------------------------------------------------- Pinnacle ---


def preserve_pinnacle_snapshot(session_id: str) -> Path:
    """Copy the current Pinnacle CSV to a permanent session-specific file."""
    source = PINNACLE_DIR / "odds.csv"

    if not source.exists():
        raise FileNotFoundError(
            f"Pinnacle odds file does not exist: {source}"
        )

    PINNACLE_RAW_DIR.mkdir(parents=True, exist_ok=True)

    destination = PINNACLE_RAW_DIR / f"pinnacle_{session_id}.csv"

    # Exclusive creation: never overwrite a previous snapshot.
    with source.open("rb") as src:
        with destination.open("xb") as dst:
            while True:
                chunk = src.read(1024 * 1024)

                if not chunk:
                    break

                dst.write(chunk)

    return destination


# ------------------------------------------------------------ subprocesses --


def start_worker(session_id: str) -> subprocess.Popen:
    """Start the EV processor for the current session."""
    print("\n=== Starting EV worker ===", flush=True)

    return subprocess.Popen(
        [
            sys.executable,
            str(WORKER_SCRIPT),
            "--session-id",
            session_id,
        ],
        cwd=ROOT,
    )


def start_monitor(
    session_id: str,
    session_start: str,
    status_file: Path,
    output_file: Path,
    args: argparse.Namespace,
) -> subprocess.Popen:
    """Start the live monitor in a separate terminal."""
    print(
        "\n=== Opening live monitor in a separate terminal ===",
        flush=True,
    )

    monitor_args = [
        "--session-id",
        session_id,
        "--session-start",
        session_start,
        "--status-file",
        str(status_file),
        "--output",
        str(output_file),
        "--top",
        str(args.top),
        "--min-ev",
        str(args.min_ev),
    ]

    if os.name == "nt":
        command = subprocess.list2cmdline(
            [
                sys.executable,
                str(MONITOR_SCRIPT),
                *monitor_args,
            ]
        )

        # /k keeps the terminal open after the monitor exits.
        return subprocess.Popen(
            [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/k",
                command,
            ],
            cwd=ROOT,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )

    return subprocess.Popen(
        [
            sys.executable,
            str(MONITOR_SCRIPT),
            *monitor_args,
        ],
        cwd=ROOT,
    )


def wait_for_worker(
    worker: subprocess.Popen,
    timeout: float = 30.0,
) -> None:
    """Ask the worker to drain its queue and stop gracefully."""
    STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    STOP_FILE.touch()

    print(
        "\nWaiting for the EV worker to drain the queue...",
        flush=True,
    )

    try:
        code = worker.wait(timeout=timeout)

        if code == 0:
            print("EV worker stopped cleanly.", flush=True)
        else:
            print(
                f"WARNING: EV worker exited with code {code}.",
                flush=True,
            )

    except subprocess.TimeoutExpired:
        print(
            "WARNING: Worker did not stop in time. "
            "Terminating it to avoid leaving a process running.",
            flush=True,
        )

        worker.terminate()

        try:
            worker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait()

    finally:
        STOP_FILE.unlink(missing_ok=True)


# ------------------------------------------------------------------- main ---


def main() -> None:
    run_clock = time.perf_counter()
    run_started_at = timestamp_string(utc_now())

    args = parse_args()

    if args.limit < 1:
        sys.exit("ERROR: --limit must be at least 1.")

    if args.offset < 0:
        sys.exit("ERROR: --offset cannot be negative.")

    if args.top < 1:
        sys.exit("ERROR: --top must be at least 1.")

    if not 0 <= args.min_ev:
        sys.exit("ERROR: --min-ev cannot be negative.")

    if not os.environ.get("PINNAPI_KEY"):
        sys.exit(
            "ERROR: PINNAPI_KEY is not set. "
            "Check your .env loading or environment variables."
        )

    for script in (
        PINNACLE_SCRIPT,
        BETCLIC_SCRIPT,
        WORKER_SCRIPT,
        MONITOR_SCRIPT,
    ):
        if not script.exists():
            sys.exit(f"ERROR: Required script not found: {script}")

    # Create the persistent directories.
    PINNACLE_RAW_DIR.mkdir(parents=True, exist_ok=True)
    BETCLIC_RAW_DIR.mkdir(parents=True, exist_ok=True)
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    # Remove a stale stop request before launching a new worker.
    STOP_FILE.unlink(missing_ok=True)

    # ----------------------------------------------------- Pinnacle snapshot

    print("\n=== Pinnacle: collecting odds ===", flush=True)

    pinnacle_clock = time.perf_counter()

    try:
        pinnacle_stats = pinnacle.main() or {}
    except Exception as exc:
        sys.exit(
            f"ERROR: Pinnacle collection failed: "
            f"{type(exc).__name__}: {exc}"
        )

    pinnacle_done_clock = time.perf_counter()
    pinnacle_finished_at = timestamp_string(utc_now())
    pinnacle_seconds = pinnacle_done_clock - pinnacle_clock

    print(
        f"--- Pinnacle finished in {pinnacle_seconds:.1f}s ---",
        flush=True,
    )

    # Create a unique session ID after Pinnacle collection.
    session_started_at = utc_now()
    session_start = timestamp_string(session_started_at)
    session_id = make_session_id()

    try:
        pinnacle_snapshot = preserve_pinnacle_snapshot(session_id)
    except (OSError, FileNotFoundError) as exc:
        sys.exit(
            f"ERROR: Could not preserve Pinnacle snapshot: {exc}"
        )

    # Every session artifact shares this ID.
    report_path = SESSIONS_DIR / f"{session_id}.txt"
    results_path = SESSIONS_DIR / f"{session_id}.csv"
    status_path = SESSIONS_DIR / f"{session_id}_status.json"

    print("\n========== NEW SESSION ==========", flush=True)
    print(f"Session ID:        {session_id}", flush=True)
    print(f"Pinnacle snapshot: {pinnacle_snapshot}", flush=True)
    print(f"Session CSV:       {results_path}", flush=True)
    print(f"Session report:    {report_path}", flush=True)

    write_json_atomic(
        status_path,
        {
            "session_id": session_id,
            "status": "running",
            "session_start": session_start,
            "pinnacle_snapshot": str(pinnacle_snapshot),
            "results": str(results_path),
            "report": str(report_path),
        },
    )

    worker = None
    monitor = None
    session_status = "finished"
    session_error = ""

    betclic_started_at = None
    betclic_finished_at = None
    betclic_clock = None
    betclic_done_clock = None
    betclic_outcome = "not started"

    try:
        # Start the worker before Betclic publishes observations.
        worker = start_worker(session_id)

        # Start the terminal monitor for this session.
        monitor = start_monitor(
            session_id=session_id,
            session_start=session_start,
            status_file=status_path,
            output_file=report_path,
            args=args,
        )

        # Give the worker a moment to initialize.
        time.sleep(1.0)

        # ------------------------------------------------ Betclic collection

        print(
            "\n=== Betclic: collecting observations ===",
            flush=True,
        )

        betclic_args = [
            "--limit",
            str(args.limit),
            "--offset",
            str(args.offset),
            "--session-id",
            session_id,
        ]

        if args.include_live:
            betclic_args.append("--include-live")

        if args.headless:
            betclic_args.append("--headless")

        command = [
            sys.executable,
            str(BETCLIC_SCRIPT),
            *betclic_args,
        ]

        print(f"Running: {' '.join(command)}", flush=True)

        betclic_started_at = timestamp_string(utc_now())
        betclic_clock = time.perf_counter()
        betclic_outcome = "failed or interrupted"

        try:
            subprocess.run(
                command,
                cwd=ROOT,
                check=True,
            )
            betclic_outcome = "completed"
        finally:
            betclic_done_clock = time.perf_counter()
            betclic_finished_at = timestamp_string(utc_now())

    except KeyboardInterrupt:
        session_status = "interrupted"
        session_error = "Workflow interrupted by user."

        print(
            "\nWorkflow interrupted by user.",
            flush=True,
        )

    except subprocess.CalledProcessError as exc:
        session_status = "failed"
        session_error = (
            f"Betclic process exited with code {exc.returncode}."
        )

        print(f"\nERROR: {session_error}", flush=True)

    except Exception as exc:
        session_status = "failed"
        session_error = f"{type(exc).__name__}: {exc}"

        print(
            f"\nERROR: Workflow failed: {session_error}",
            flush=True,
        )

    finally:
        # Request a graceful worker shutdown only if it was started.
        if worker is not None:
            wait_for_worker(worker)

        finished_at = timestamp_string(utc_now())

        performance = {
            "run_started_at": run_started_at,
            "run_finished_at": finished_at,
            "total_seconds": time.perf_counter() - run_clock,
            "pinnacle": {
                "total_seconds": pinnacle_seconds,
                "api_seconds": pinnacle_stats.get("api_seconds"),
                "events": pinnacle_stats.get("events"),
                "matches": pinnacle_stats.get("matches"),
                "completed_at": pinnacle_finished_at,
            },
            "betclic": {
                "seconds": (
                    betclic_done_clock - betclic_clock
                    if betclic_clock is not None
                    and betclic_done_clock is not None
                    else None
                ),
                "started_at": betclic_started_at,
                "completed_at": betclic_finished_at,
                "outcome": betclic_outcome,
            },
            "completion_gap_seconds": (
                betclic_done_clock - pinnacle_done_clock
                if betclic_done_clock is not None
                else None
            ),
        }

        write_json_atomic(
            status_path,
            {
                "session_id": session_id,
                "status": session_status,
                "session_start": session_start,
                "session_finished_at": finished_at,
                "pinnacle_snapshot": str(pinnacle_snapshot),
                "results": str(results_path),
                "report": str(report_path),
                "error": session_error,
                "performance": performance,
            },
        )

        print("\n========== SESSION FINISHED ==========", flush=True)
        print(f"Session ID:        {session_id}", flush=True)
        print(f"Session status:    {session_status}", flush=True)
        print(f"Pinnacle snapshot: {pinnacle_snapshot}", flush=True)
        print(f"Session CSV:       {results_path}", flush=True)
        print(f"Session report:    {report_path}", flush=True)

        print("", flush=True)

        for line in performance_lines(performance):
            print(line, flush=True)

        if monitor is not None:
            print(
                "The monitor terminal was opened separately. "
                "It may still be displaying the final report.",
                flush=True,
            )

    print("\nWorkflow finished.", flush=True)


if __name__ == "__main__":
    main()