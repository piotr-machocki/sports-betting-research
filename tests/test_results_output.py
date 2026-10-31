"""Session result outputs: per-method EV columns, live table, raw data and
display reports (scripts/process_live_odds.py, live_monitor.py,
results_store.py). No network, no browser; files go to pytest's tmp_path.
"""

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import live_monitor  # noqa: E402
import process_live_odds  # noqa: E402
import results_store  # noqa: E402
from betting.margin import METHODS  # noqa: E402
from betting.value import ev_by_method  # noqa: E402

SESSION = "session_20261030_120000_abc123"
START = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 30, 12, 10, tzinfo=timezone.utc)


def message(**overrides) -> dict:
    data = {
        "session_id": SESSION,
        "observation_id": "obs1",
        "match_no": 1,
        "pinnacle_captured_at": "2026-10-30T12:00:00+00:00",
        "betclic_captured_at": "2026-10-30T12:01:00+00:00",
        "pinnacle_player1": "Alice Alpha",
        "pinnacle_player2": "Bella Beta",
        "pinnacle_tournament": "WTA Test - R1",
        "pinnacle_odds1": 1.50,
        "pinnacle_odds2": 2.70,
        "betclic_player1": "A. Alpha",
        "betclic_player2": "B. Beta",
        "betclic_odds_for_pin_p1": 1.55,
        "betclic_odds_for_pin_p2": 2.60,
        "status": "FOUND",
        "odds_ok": True,
        "confidence": "good",
        "betclic_status": "not_marked_live",
        "url": "https://example.invalid/match",
        "note": "",
    }
    data.update(overrides)
    return data


def as_csv_row(result: dict) -> dict:
    """A worker result as the monitor reads it back from the session CSV."""
    return {key: "" if value is None else str(value) for key, value in result.items()}


def session_rows() -> list[dict]:
    """Positive EV, negative EV and a NOT FOUND match."""
    return [
        as_csv_row(process_live_odds.calculate_observation(message())),
        as_csv_row(process_live_odds.calculate_observation(message(
            observation_id="obs2",
            match_no=2,
            pinnacle_player1="Cara Gamma",
            pinnacle_player2="Dana Delta",
            pinnacle_odds1=1.80,
            pinnacle_odds2=2.05,
            betclic_odds_for_pin_p1=1.70,
            betclic_odds_for_pin_p2=1.95,
        ))),
        as_csv_row(process_live_odds.calculate_observation(message(
            observation_id="obs3",
            match_no=3,
            pinnacle_player1="Erin Epsilon",
            pinnacle_player2="Fay Zeta",
            status="NOT FOUND",
            betclic_odds_for_pin_p1="",
            betclic_odds_for_pin_p2="",
        ))),
    ]


def finished_info() -> dict:
    return {
        "session_id": SESSION,
        "status": "finished",
        "session_start": "2026-10-30T12:00:00Z",
        "session_finished_at": "2026-10-30T12:05:00Z",
    }


def args() -> object:
    return results_store.report_namespace(SESSION)


# ------------------------------------------------------------- worker ------


def test_worker_stores_every_method_and_keeps_reference_columns():
    result = process_live_odds.calculate_observation(message())
    expected = ev_by_method([1.50, 2.70], [1.55, 2.60])

    for method in METHODS:
        probabilities, evs = expected[method]

        for side in (1, 2):
            assert result[results_store.probability_column(method, side)] == (
                pytest.approx(probabilities[side - 1], abs=1e-6)
            )
            assert result[results_store.ev_column(method, side)] == (
                pytest.approx(evs[side - 1], abs=1e-6)
            )

    # Unchanged meaning: the unsuffixed columns are the proportional method.
    assert result["probability1"] == pytest.approx(
        expected["proportional"][0][0], abs=1e-6
    )
    assert result["best_ev"] == max(result["ev1"], result["ev2"])
    assert set(results_store.METHOD_FIELDS) <= set(
        process_live_odds.OUTPUT_FIELDS
    )


def test_worker_leaves_method_columns_blank_when_not_calculated():
    result = process_live_odds.calculate_observation(
        message(status="NOT FOUND")
    )

    assert all(result[column] == "" for column in results_store.METHOD_FIELDS)
    assert result["best_ev"] == ""


def test_complete_methods_backfills_old_session_rows_like_the_worker():
    new_row = as_csv_row(process_live_odds.calculate_observation(message()))
    old_row = {
        key: value
        for key, value in new_row.items()
        if key not in results_store.METHOD_FIELDS
    }

    filled = results_store.complete_methods(old_row)

    for column in results_store.METHOD_FIELDS:
        assert float(filled[column]) == pytest.approx(float(new_row[column]))

    not_found = {"ev1": "", "pinnacle_odds1": "1.5"}
    assert results_store.complete_methods(not_found) is not_found


# ------------------------------------------------------------ live table ---


def test_table_shows_all_four_ev_columns_for_the_bet_on_side():
    rows = sorted(session_rows(), key=live_monitor.sort_key)

    with live_monitor.plain():
        header, _, *lines = live_monitor.table_lines(rows, args(), NOW)

    for label in ("EV(ref)", "EV(pow)", "EV(shin)", "EV(OR)", "GAP", "AGE",
                  "P(ref)", "Betclic", "Pinnacle", "CONF", "OPPONENT"):
        assert label in header

    # Every match is listed, negative EV and not found included, in the
    # existing order (best reference EV first, uncalculated last).
    assert len(lines) == 3
    assert "Alice Alpha" in lines[0]
    assert "Cara Gamma" in lines[1] or "Dana Delta" in lines[1]
    assert "NOT FOUND" in lines[2]

    row = rows[0]
    side = live_monitor.best_side(row)
    for method in METHODS:
        value = float(row[results_store.ev_column(method, side)])
        assert f"{value * 100:+.2f}%" in lines[0]

    # Betclic captured 12:01, clock 12:10 -> 9 minutes old.
    assert "9 min 00 s" in lines[0]
    assert lines[2].count("--") >= 4 + 3  # four EVs, odds, probability


# ---------------------------------------------------------- raw records ----


def test_raw_records_have_every_value_in_a_fixed_schema():
    records = results_store.raw_records(session_rows(), SESSION)

    assert [list(record) for record in records] == [
        results_store.RAW_FIELDS
    ] * 3

    first = records[0]
    assert first["session_id"] == SESSION
    assert first["latest_for_match"] is True
    assert first["pinnacle_margin"] == pytest.approx(1 / 1.5 + 1 / 2.7 - 1, abs=1e-6)
    assert first["betclic_margin"] == pytest.approx(1 / 1.55 + 1 / 2.6 - 1, abs=1e-6)

    for method in METHODS:
        for side in (1, 2):
            assert first[f"ev{side}_{method}"] != ""
            assert first[f"probability{side}_{method}"] != ""

    assert records[2]["status"] == "NOT FOUND"
    assert records[2]["ev1_power"] == ""
    assert records[2]["best_side"] == ""


def test_latest_for_match_marks_only_the_newest_observation():
    rows = session_rows()
    repeat = dict(rows[0], observation_id="obs1b",
                  betclic_captured_at="2026-10-30T12:03:00+00:00")

    records = results_store.raw_records(rows + [repeat], SESSION)
    flags = {r["observation_id"]: r["latest_for_match"] for r in records}

    assert flags["obs1"] is False
    assert flags["obs1b"] is True


# -------------------------------------------------------------- export -----


def export(tmp_path: Path, session_id: str = SESSION, **kwargs) -> dict:
    return results_store.export_session(
        session_id,
        results_store.report_namespace(session_id),
        rows=kwargs.pop("rows", session_rows()),
        info=kwargs.pop("info", finished_info()),
        raw_dir=tmp_path / "raw",
        reports_dir=tmp_path / "reports",
        now=NOW,
        **kwargs,
    )


def test_export_writes_raw_csv_and_json(tmp_path):
    written = export(tmp_path)

    assert written["raw_csv"] == tmp_path / "raw" / f"{SESSION}.csv"
    assert written["raw_json"] == tmp_path / "raw" / f"{SESSION}.json"

    with written["raw_csv"].open(encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        assert reader.fieldnames == results_store.RAW_FIELDS
        rows = list(reader)

    assert [row["observation_id"] for row in rows] == ["obs1", "obs2", "obs3"]
    assert float(rows[0]["ev1_shin"]) == pytest.approx(
        ev_by_method([1.50, 2.70], [1.55, 2.60])["shin"][1][0], abs=1e-6
    )

    document = json.loads(written["raw_json"].read_text(encoding="utf-8"))

    assert document["session_id"] == SESSION
    assert document["session_status"] == "finished"
    assert document["reference_method"] == "proportional"
    assert document["methods"] == list(METHODS)
    assert document["fields"] == results_store.RAW_FIELDS

    first, _, missing = document["observations"]
    assert isinstance(first["ev1_odds_ratio"], float)
    assert isinstance(first["match_no"], int)
    assert first["best_side"] == 1
    assert missing["ev1_odds_ratio"] is None
    assert missing["best_side"] is None


def test_export_writes_display_report_and_latest(tmp_path):
    written = export(tmp_path)

    report = written["report"].read_text(encoding="utf-8")

    assert written["report"] == tmp_path / "reports" / f"{SESSION}.txt"
    assert written["latest_report"].read_text(encoding="utf-8") == report
    assert "\x1b[" not in report  # plain text, no ANSI colours

    with live_monitor.plain():
        expected_table = live_monitor.table_lines(
            sorted(session_rows(), key=live_monitor.sort_key),
            results_store.report_namespace(SESSION),
            live_monitor.session_clock(finished_info(), NOW),
        )

    # Same layout as the terminal table, every match included.
    assert "\n".join(expected_table) in report
    for name in ("Alice Alpha", "Erin Epsilon"):
        assert name in report
    assert "LIVE SESSION   FINISHED" in report


def test_export_keeps_previous_reports_and_moves_latest(tmp_path):
    export(tmp_path)

    second = "session_20261030_130000_def456"
    export(tmp_path, session_id=second)

    reports = tmp_path / "reports"
    assert (reports / f"{SESSION}.txt").exists()
    assert (reports / f"{second}.txt").exists()
    assert f"Session: {second}" in (reports / "latest.txt").read_text(
        encoding="utf-8"
    )

    export(tmp_path, update_latest=False)
    assert f"Session: {second}" in (reports / "latest.txt").read_text(
        encoding="utf-8"
    )


def test_export_reads_session_files_when_rows_are_not_given(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()

    rows = session_rows()
    with (sessions / f"{SESSION}.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as file:
        writer = csv.DictWriter(file, fieldnames=process_live_odds.OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    (sessions / f"{SESSION}_status.json").write_text(
        json.dumps(finished_info()), encoding="utf-8"
    )

    written = results_store.export_session(
        SESSION,
        results_store.report_namespace(SESSION),
        sessions_dir=sessions,
        raw_dir=tmp_path / "raw",
        reports_dir=tmp_path / "reports",
        now=NOW,
    )

    document = json.loads(written["raw_json"].read_text(encoding="utf-8"))
    assert len(document["observations"]) == 3


# ------------------------------------------------- existing session report -


def test_session_report_still_final_and_lists_every_method(tmp_path):
    text = live_monitor.render_report(
        session_rows(), args(), START, finished_info(), NOW
    )

    # run_once.ensure_report() relies on these two markers.
    assert "END OF SESSION REPORT" in text
    assert "LIVE SESSION   FINISHED" in text

    for label in ("EV(ref):", "EV(pow):", "EV(shin):", "EV(OR):"):
        assert label in text
