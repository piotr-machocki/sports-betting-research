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
    8. Save the raw data (data/results/raw/) and the display report
       (data/results/reports/) of the session.

Usage:
    python scripts/run_once.py
    python scripts/run_once.py --limit 20
    python scripts/run_once.py --limit 20 --min-ev 0.02 --top 20
    python scripts/run_once.py --limit 20 --include-live

Without --limit every available Pinnacle match is processed.
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
import results_store  # noqa: E402


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


def build_pinnacle_info(
    stats: dict,
    total_seconds: float,
    completed_at: str,
) -> dict:
    """Pinnacle collection summary (timings and data-quality breakdown)."""
    return {
        "total_seconds": total_seconds,
        "api_seconds": stats.get("api_seconds"),
        "events": stats.get("events"),
        "matches": stats.get("matches"),
        "excluded": stats.get("excluded"),
        "invalid": stats.get("invalid"),
        "completed_at": completed_at,
    }


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
        default=None,
        help="Number of Pinnacle matches to test. Default: all.",
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


def pending_messages() -> int:
    """Number of queue messages still waiting in the incoming directory."""
    try:
        return sum(1 for _ in INCOMING_DIR.glob("*.json"))
    except OSError:
        return 0


def wait_for_worker(
    worker: subprocess.Popen,
    timeout: float = 30.0,
) -> None:
    """Ask the worker to drain its queue and stop gracefully.

    `timeout` is an IDLE timeout: it restarts whenever the queue shrinks, so
    a long queue is never cut off while the worker is still making progress.
    """
    STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    STOP_FILE.touch()

    print(
        "\nWaiting for the EV worker to drain the queue...",
        flush=True,
    )

    try:
        last_pending = pending_messages()
        last_progress = time.monotonic()

        while True:
            code = worker.poll()

            if code is not None:
                if code == 0:
                    print("EV worker stopped cleanly.", flush=True)
                else:
                    print(
                        f"WARNING: EV worker exited with code {code}.",
                        flush=True,
                    )

                return

            pending = pending_messages()

            if pending != last_pending:
                last_pending = pending
                last_progress = time.monotonic()

            elif time.monotonic() - last_progress > timeout:
                break

            time.sleep(0.25)

        print(
            f"WARNING: Worker made no progress for {timeout:g}s "
            f"({last_pending} message(s) pending). "
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


def ensure_report(
    session_id: str,
    session_start: str,
    status_path: Path,
    report_path: Path,
    results_path: Path,
    args: argparse.Namespace,
    wait: float = 10.0,
) -> None:
    """Make sure the session report reflects the final session status.

    The monitor normally writes it. If the monitor was closed or crashed,
    write the report here from the same session CSV and status file.
    """
    try:
        status = str(
            json.loads(status_path.read_text(encoding="utf-8"))["status"]
        ).upper()
    except (OSError, ValueError, KeyError):
        status = ""

    def report_is_final() -> bool:
        try:
            text = report_path.read_text(encoding="utf-8")
        except OSError:
            return False

        return (
            "END OF SESSION REPORT" in text
            and f"LIVE SESSION   {status}" in text
        )

    deadline = time.monotonic() + wait

    while time.monotonic() < deadline:
        if report_is_final():
            return

        time.sleep(0.25)

    if report_is_final():
        return

    print(
        "Monitor did not write the final report; writing it here.",
        flush=True,
    )

    try:
        import live_monitor

        info = live_monitor.read_runner_info(status_path)
        rows = live_monitor.read_session_rows(results_path) or []

        text = live_monitor.render_report(
            rows,
            results_store.report_namespace(session_id, args.min_ev),
            live_monitor.parse_timestamp(session_start),
            info,
            utc_now(),
        )

        live_monitor.write_report(report_path, text)

    except Exception as exc:
        print(
            f"WARNING: could not write the session report: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


def save_results(session_id: str, args: argparse.Namespace) -> dict:
    """Write the session's raw data and display report; never raises."""
    try:
        return results_store.export_session(
            session_id,
            results_store.report_namespace(session_id, args.min_ev),
        )

    except Exception as exc:
        print(
            f"WARNING: could not save the session results: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )

        return {}


# ------------------------------------------------------------------- main ---


def main() -> None:
    run_clock = time.perf_counter()
    run_started_at = timestamp_string(utc_now())

    args = parse_args()

    if args.limit is not None and args.limit < 1:
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

    # Everything about the Pinnacle collection is known now. It goes into the
    # status file the monitor reads, so PINNACLE DATA QUALITY can be shown
    # before Betclic scraping starts. No further Pinnacle request is made.
    pinnacle_info = build_pinnacle_info(
        pinnacle_stats,
        pinnacle_seconds,
        pinnacle_finished_at,
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
            "pinnacle": pinnacle_info,
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
            "--offset",
            str(args.offset),
            "--session-id",
            session_id,
        ]

        if args.limit is not None:
            betclic_args += ["--limit", str(args.limit)]

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
        # Whatever the scraper's exit status, everything it already
        # published is processed: request a graceful worker shutdown
        # (queue drain) only if the worker was started.
        if worker is not None:
            wait_for_worker(worker)

        finished_at = timestamp_string(utc_now())

        performance = {
            "run_started_at": run_started_at,
            "run_finished_at": finished_at,
            "total_seconds": time.perf_counter() - run_clock,
            "pinnacle": pinnacle_info,
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

        # The monitor writes the report; fall back to writing it here.
        ensure_report(
            session_id=session_id,
            session_start=session_start,
            status_path=status_path,
            report_path=report_path,
            results_path=results_path,
            args=args,
        )

        saved = save_results(session_id, args)

        print("\n========== SESSION FINISHED ==========", flush=True)
        print(f"Session ID:        {session_id}", flush=True)
        print(f"Session status:    {session_status}", flush=True)
        print(f"Pinnacle snapshot: {pinnacle_snapshot}", flush=True)
        print(f"Session CSV:       {results_path}", flush=True)
        print(f"Session report:    {report_path}", flush=True)

        for label, key in (
            ("Raw data (CSV):", "raw_csv"),
            ("Raw data (JSON):", "raw_json"),
            ("Display report:", "report"),
            ("Latest report:", "latest_report"),
        ):
            if key in saved:
                print(f"{label:<19}{saved[key]}", flush=True)

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