import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import dashboard_runs  # noqa: E402
import run_ledger  # noqa: E402

LOG = """===== 2026-11-15 00:00:04 UTC =====

=== Pinnacle: collecting odds ===
--- Pinnacle finished in 0.6s ---
Session ID:        session_20261115_000004_4d40d1
Superbet: 201 events, 44 of 45 Pinnacle matches found, 0 with EV > 0 after tax. x.csv
WARNING: competition 36888 skipped: grpc-status 5 from https://example
polymarket: 30 matches, 28 with both asks
WARNING: kalshi failed: ReadTimeout: slow
exit code 0
===== 2026-11-17 18:05:01 UTC =====

=== Pinnacle: collecting odds ===
ERROR: Pinnacle collection failed: HTTPError: 403 Client Error: Forbidden for url: https://pinnapi.com/kit/v1/
exit code 1
===== 2026-11-17 18:09:59 UTC: previous run still going, skipped =====
===== 2026-11-20 10:00:02 UTC =====
Pinnacle skipped: PINNFEED_KEY is not set.
Run ID: books_20261120_100002_abc123
Superbet: 255 events, feed saved (no Pinnacle list). x.json.gz
WARNING: Betclic collection failed: Command returned non-zero exit status 1.
Books saved: Superbet
exit code 0
"""


def status_file(folder: Path, session_id: str, phases: list[dict]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{session_id}_status.json").write_text(json.dumps({
        "status": "finished",
        "performance": {"betclic": {"outcome": "completed", "phases": phases}},
    }))


def test_backfill_rebuilds_each_logged_run(tmp_path, monkeypatch):
    monkeypatch.setattr(run_ledger, "SESSIONS_DIR", tmp_path / "sessions")
    status_file(tmp_path / "sessions", "session_20261115_000004_4d40d1",
                [{"name": "feed", "error": "competition 36888 skipped: grpc-status 5"}])
    log = tmp_path / "scheduled_run.log"
    log.write_text(LOG, encoding="utf-8")
    ledger = tmp_path / "runs.jsonl"
    # A live record already there: only the runs before it are added.
    run_ledger.record("session_x", "session", "2026-11-20T12:00:00Z", "finished",
                      {"pinnacle": run_ledger.state("ok")}, ledger=ledger)

    assert run_ledger.backfill([log], ledger) == 4
    assert run_ledger.backfill([log], ledger) == 0

    session, failed, skipped, books, live = run_ledger.read(ledger)
    states = lambda run: {name: each["state"] for name, each in run["sources"].items()}  # noqa: E731
    assert session["run_id"] == "session_20261115_000004_4d40d1" and session["from_log"]
    assert states(session) == {"pinnacle": "ok", "betclic": "partial", "superbet": "ok",
                               "polymarket": "ok", "kalshi": "failed"}
    assert failed["kind"] == "failed" and states(failed)["pinnacle"] == "failed"
    assert "403" in failed["sources"]["pinnacle"]["detail"]
    assert skipped["kind"] == "skipped"
    assert books["kind"] == "books" and states(books) == {
        "pinnacle": "skipped", "betclic": "failed", "superbet": "ok",
        "polymarket": "skipped", "kalshi": "skipped"}
    assert live["run_id"] == "session_x"


def test_betclic_state_from_the_status_performance():
    assert run_ledger.betclic_state({"betclic": {"outcome": "completed", "phases": [
        {"name": "feed"}]}}) == {"state": "ok"}
    assert run_ledger.betclic_state({"betclic": {"outcome": "completed", "phases": [
        {"name": "feed", "error": "FeedError"}, {"name": "tournaments"}]}})["state"] == "partial"
    assert run_ledger.betclic_state({"betclic": {"outcome": "failed or interrupted"}})["state"] == "failed"


def test_market_states_read_the_saved_snapshot(tmp_path, monkeypatch):
    import gzip

    monkeypatch.setattr(run_ledger, "MARKETS_DIR", tmp_path)
    (tmp_path / "2026-11-20").mkdir()
    with gzip.open(tmp_path / "2026-11-20" / "run1.csv.gz", "wt") as file:
        file.write("source,player1\npolymarket,A\n")

    states = run_ledger.market_states("run1", tried=True)
    assert states["polymarket"]["state"] == "ok" and states["kalshi"]["state"] == "failed"
    assert run_ledger.market_states("run2", tried=True)["polymarket"]["state"] == "failed"
    assert run_ledger.market_states("run1", tried=False)["kalshi"]["state"] == "skipped"


def run(hours_ago, now, **states):
    return {
        "run_id": f"r{hours_ago}",
        "kind": "session",
        "started_at": (now - timedelta(hours=hours_ago)).isoformat(),
        "sources": {name: run_ledger.state(value) for name, value in states.items()},
    }


def test_health_rates_count_only_runs_that_tried_a_source():
    now = datetime(2026, 11, 20, 18, tzinfo=timezone.utc)
    runs = [
        run(48, now, pinnacle="failed", superbet="skipped"),
        run(3, now, pinnacle="ok", superbet="partial"),
        run(2, now, pinnacle="ok", superbet="failed"),
        {"kind": "skipped", "started_at": (now - timedelta(hours=1)).isoformat(), "sources": {}},
    ]
    stale = timedelta(minutes=20)

    day = dashboard_runs.health(runs, ["pinnacle", "superbet"], now, "24 h", stale)
    whole, pinnacle, superbet = (card["stats"] for card in day["cards"])
    assert (whole["worked"], whole["tried"]) == (1, 3)
    assert (pinnacle["worked"], pinnacle["tried"]) == (2, 2)
    assert (superbet["worked"], superbet["tried"], superbet["partial"]) == (1, 2, 1)
    assert (day["runs"], day["skipped"]) == (3, 1)
    # A green card still names the newest failure ever.
    assert day["cards"][1]["last_failure"][0] == now - timedelta(hours=48)

    week = dashboard_runs.health(runs, ["pinnacle", "superbet"], now, "7 days", stale)
    assert (week["cards"][1]["stats"]["worked"], week["cards"][1]["stats"]["tried"]) == (2, 3)
    assert week["longest_gap"] == timedelta(hours=45)

    # 24 hourly buckets: the run 3 h ago worked, the one 2 h ago failed Superbet.
    bars = day["cards"][2]["buckets"]
    assert len(bars) == 24 and [b["tried"] for b in bars[-3:]] == [1, 1, 0]
    assert [b["worked"] for b in bars[-3:-1]] == [1, 0]

    # Latest attempts: Pinnacle worked 2 h ago (older than 20 min = no recent run).
    assert day["cards"][1]["status"] == "stale"
    fresh = dashboard_runs.health(runs, ["pinnacle"], now, "24 h", timedelta(hours=3))
    assert fresh["cards"][1]["status"] == "ok"

    page = dashboard_runs.sport_html("Tennis", fresh, dashboard_runs.TENNIS_LABELS, "24 h", now)
    assert "Whole run" in page and "Pinnacle" in page and "health-bar bad" in page
    assert "All sources working" in page and "skipped by cron" in page
    assert "1 of 1 worked" in page


def test_sport_pill_names_failing_sources():
    now = datetime(2026, 11, 20, 18, tzinfo=timezone.utc)
    runs = [run(0.05, now, pinnacle="failed", superbet="ok")]
    result = dashboard_runs.health(runs, ["pinnacle", "superbet"], now, "24 h", timedelta(minutes=20))
    page = dashboard_runs.sport_html("Tennis", result, dashboard_runs.TENNIS_LABELS, "24 h", now)
    assert "Failing: Pinnacle" in page and "Failing</span>" in page


def test_nba_runs_map_errors_and_skips(tmp_path):
    path = tmp_path / "runs.jsonl"
    path.write_text(json.dumps({"run_id": "nba_1", "captured_at": "2026-11-17T18:07:01Z", "sources": {
        "pinnacle": {"error": "RuntimeError: HTTP 403", "seconds": 0.1},
        "espn": {"rows": 20, "seconds": 0.6},
        "espn_props": {"skipped": "props run only", "seconds": 0.0},
    }}) + "\n")

    [nba] = dashboard_runs.nba_runs(path)
    assert {name: each["state"] for name, each in nba["sources"].items()} == {
        "pinnacle": "failed", "espn": "ok", "espn_props": "skipped"}
    assert not dashboard_runs.run_worked(nba)
