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

    # The unsuffixed columns are the power (reference) method.
    assert results_store.REFERENCE_METHOD == "power"
    assert result["probability1"] == pytest.approx(
        expected["power"][0][0], abs=1e-6
    )
    assert result["ev1"] == pytest.approx(expected["power"][1][0], abs=1e-6)
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


def test_complete_methods_converts_rows_with_the_legacy_reference():
    # Before the switch the unsuffixed columns held proportional values and
    # the other methods were suffixed (no *_proportional columns).
    new_row = as_csv_row(process_live_odds.calculate_observation(message()))
    legacy = ev_by_method([1.50, 2.70], [1.55, 2.60])
    old_row = {
        key: value
        for key, value in new_row.items()
        if not key.endswith("_proportional")
    }
    old_row.update({
        "probability1": str(round(legacy["proportional"][0][0], 6)),
        "probability2": str(round(legacy["proportional"][0][1], 6)),
        "ev1": str(round(legacy["proportional"][1][0], 6)),
        "ev2": str(round(legacy["proportional"][1][1], 6)),
        "best_ev": str(round(max(legacy["proportional"][1]), 6)),
        "probability1_power": str(round(legacy["power"][0][0], 6)),
        "ev1_power": str(round(legacy["power"][1][0], 6)),
    })

    filled = results_store.complete_methods(old_row)

    for column in (*results_store.METHOD_FIELDS, "probability1", "ev1",
                   "ev2", "best_ev"):
        assert float(filled[column]) == pytest.approx(float(new_row[column]))
    assert filled["best_player"] == new_row["best_player"]


def test_old_historical_csv_is_upgraded_and_recalculated(tmp_path):
    path = tmp_path / "processed_odds.csv"
    new_row = as_csv_row(process_live_odds.calculate_observation(message()))
    legacy = ev_by_method([1.50, 2.70], [1.55, 2.60])["proportional"]
    old_header = [
        column for column in process_live_odds.OUTPUT_FIELDS
        if column not in results_store.METHOD_FIELDS and column != "match_no"
    ] + ["old_extra"]
    old_row = dict(new_row, ev1=str(legacy[1][0]), ev2=str(legacy[1][1]),
                   best_ev=str(max(legacy[1])), old_extra="x")
    not_found = {"observation_id": "obs9", "status": "NOT FOUND", "ev1": ""}

    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=old_header,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows([old_row, not_found])

    assert process_live_odds.upgrade_historical_csv(path) is True
    assert process_live_odds.upgrade_historical_csv(path) is False
    assert (tmp_path / "processed_odds.legacy_backup.csv").exists()

    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        assert reader.fieldnames == process_live_odds.OUTPUT_FIELDS + ["old_extra"]
        upgraded, missing = list(reader)

    for column in (*results_store.METHOD_FIELDS, "ev1", "ev2", "best_ev"):
        assert float(upgraded[column]) == pytest.approx(float(new_row[column]))
    assert upgraded["old_extra"] == "x"
    assert missing["status"] == "NOT FOUND" and missing["ev1"] == ""

    # Appends now use the full header.
    assert process_live_odds.csv_fieldnames(path)[:len(
        process_live_odds.OUTPUT_FIELDS)] == process_live_odds.OUTPUT_FIELDS


def test_header_is_read_once_per_file_and_again_after_an_upgrade(tmp_path, monkeypatch):
    path = tmp_path / "session.csv"
    reads = []
    real = process_live_odds.csv_fieldnames

    def counted(target):
        reads.append(target)
        return real(target)

    monkeypatch.setattr(process_live_odds, "csv_fieldnames", counted)
    monkeypatch.setattr(process_live_odds, "CSV_HEADERS", {})

    for number in range(3):
        process_live_odds.append_csv_row(path, {"observation_id": str(number)})

    assert reads == [path]

    with path.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert [row["observation_id"] for row in rows] == ["0", "1", "2"]

    # An existing file keeps its own columns, also when first seen in this run.
    old = tmp_path / "old.csv"
    old.write_text("observation_id,old_extra\n1,x\n", encoding="utf-8")
    process_live_odds.append_csv_row(old, {"observation_id": "2", "old_extra": "y"})
    process_live_odds.append_csv_row(old, {"observation_id": "3", "old_extra": "z"})
    assert old.read_text(encoding="utf-8").splitlines()[1:] == ["1,x", "2,y", "3,z"]

    # upgrade_historical_csv rewrites the header, so it must be read again.
    process_live_odds.upgrade_historical_csv(old)
    assert old not in process_live_odds.CSV_HEADERS


# ------------------------------------------------------------ live table ---


def test_table_shows_all_four_ev_columns_for_the_bet_on_side():
    rows = sorted(session_rows(), key=live_monitor.sort_key)

    with live_monitor.plain():
        header, _, *lines = live_monitor.table_lines(rows, args())

    for label in ("EV(ref)", "EV(OR)", "EV(shin)", "EV(prop)", "GAP",
                  "P(ref)", "Betc", "Pinn", "CONF", "OPPONENT",
                  "Mb", "Mp", "TOURNAMENT"):
        assert label in header

    assert "FLAGS" not in header

    # Requested order: opponent next to BET ON, margins right after the odds,
    # tournament last.
    assert header.index("BET ON") < header.index("OPPONENT") < header.index("Betc")
    assert header.index("Pinn") < header.index(" Mb ") < header.index(" Mp ")
    assert header.rstrip().endswith("TOURNAMENT")

    # Every match is listed, negative EV and not found included, in the
    # existing order (best reference EV first, uncalculated last).
    assert len(lines) == 3
    assert "A. Alpha" in lines[0] and "B. Beta" in lines[0]
    assert "C. Gamma" in lines[1] or "D. Delta" in lines[1]
    assert lines[2][4] == "N"
    assert lines[0][4] == " "
    assert all(line.endswith("WTA Test R1") for line in lines)

    # Margins: 1/1.55 + 1/2.60 - 1 and 1/1.50 + 1/2.70 - 1.
    assert "2.98%" in lines[0] and "3.70%" in lines[0]
    # Without Betclic odds only the Pinnacle margin is known.
    assert "3.70%" in lines[2]

    row = rows[0]
    side = live_monitor.best_side(row)
    for method in METHODS:
        value = float(row[results_store.ev_column(method, side)])
        assert f"{value * 100:+.2f}%" in lines[0]

    # GAP: Betclic captured one minute after Pinnacle; no AGE column.
    assert "1m00s" in lines[0]
    assert "9m00s" not in lines[0]
    assert lines[2].count("--") >= 4 + 3 + 1  # EVs, odds, probability, margin


@pytest.mark.parametrize("name, expected", [
    ("Carlos Alcaraz", "C. Alcaraz"),
    ("Alejandro Davidovich Fokina", "A. Fokina"),
    ("Jan-Lennard Struff", "J. Struff"),
    ("Austin Krajicek / Nikola Mektic", "Krajicek/Mektic"),
    ("Aneta Kucmova / Aneta Laboutkova", "Kucmova/Laboutko"),
    ("Polina Kudermetova / Maria Timofeeva", "Kuderme/Timofeev"),
    ("Sinner", "Sinner"),
    ("", "?"),
    (None, "?"),
])
def test_short_name(name, expected):
    assert live_monitor.short_name(name) == expected
    assert len(expected) <= live_monitor.PW


@pytest.mark.parametrize("seconds, expected", [
    (19, "19s"),
    (65, "1m05s"),
    (3720, "1h02m"),
])
def test_fmt_gap_fits_the_column(seconds, expected):
    assert live_monitor.fmt_gap(seconds) == expected


@pytest.mark.parametrize("name, expected", [
    ("ATP Challenger Bratislava - Qualifiers", "CH Bratislava Q"),
    ("ATP Challenger Costa do Sauipe - R1", "CH Costa do Sauipe R1"),
    ("WTA 125k Cali - R1", "W125 Cali R1"),
    ("WTA 125K Courmayeur - Doubles", "W125 Courmayeur D"),
    ("ITF Men Trois-Rivieres - R16", "ITF M Trois-Rivieres R16"),
    ("ITF Women New Braunfels - Final", "ITF W New Braunfels F"),
    ("ATP Paris - SF", "ATP Paris SF"),
    ("WTA Hong Kong - QF", "WTA Hong Kong QF"),
    ("Some Exhibition", "Some Exhibition"),
    ("", ""),
    (None, ""),
])
def test_short_tournament(name, expected):
    assert live_monitor.short_tournament(name) == expected


@pytest.mark.parametrize("overrides, mark", [
    ({"status": "NOT FOUND"}, "N"),
    ({"status": "VERIFY"}, "V"),
    ({"status": "AMBIGUOUS"}, "A"),
    ({"status": "ERROR"}, "E"),
    ({"betclic_status": "live"}, "L"),
    ({"odds_ok": False}, "X"),
])
def test_status_mark_says_why_a_match_has_no_ev(overrides, mark):
    row = as_csv_row(process_live_odds.calculate_observation(message(**overrides)))

    with live_monitor.plain():
        assert live_monitor.status_mark(row) == mark


def test_doubles_take_one_line_and_the_row_stays_in_the_terminal():
    row = as_csv_row(process_live_odds.calculate_observation(message(
        pinnacle_player1="Francisco Cerundolo / Tomas Martin Etcheverry",
        pinnacle_player2="Austin Krajicek / Nikola Mektic",
        pinnacle_tournament="ITF Women New Braunfels - R16",
        confidence="manual_review",
    )))

    with live_monitor.plain():
        header, rule, line = live_monitor.table_lines([row], args())

    assert "Cerundo/Etchever" in line and "Krajicek/Mektic" in line
    assert line.endswith("ITF W New Braunfels R16")

    # The user's full-screen terminal is 156 columns wide.
    assert max(map(len, (header, rule, line))) < 156

    # The vertical bars line up on every line.
    def bars(text, bar="|"):
        return [position for position, char in enumerate(text) if char == bar]

    assert len(bars(header)) == 14
    assert bars(line) == bars(header) == bars(rule, "+")
    assert "| manual|" in line


def test_legend_has_one_topic_per_line_with_aligned_labels():
    with live_monitor.plain():
        title, *lines = live_monitor.legend_lines(args())

    assert title == "Legend:"

    labels = [line[:2 + live_monitor.LEGEND_LABEL_W].strip() for line in lines]
    assert labels == [
        "BET ON", "Rows", "Betc / Pinn", "Mb / Mp", "Devig", "EV colours",
        "GAP", "CONF", "No-EV letter", "TOURNAMENT", "Round",
    ]

    # The text after the label starts in the same column on every line.
    start = 2 + live_monitor.LEGEND_LABEL_W + 2
    assert all(line[start - 1] == " " and line[start] != " " for line in lines)

    # The user's full-screen terminal is 156 columns wide.
    assert max(map(len, lines)) < 156


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
    assert document["reference_method"] == "power"
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
        )

    # Same layout as the terminal table, every match included.
    assert "\n".join(expected_table) in report
    for name in ("A. Alpha", "E. Epsilon"):
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

    for label in ("EV(ref):", "EV(OR):", "EV(shin):", "EV(prop):"):
        assert label in text


# ----------------------------------- match identity, start time, margins ---


def test_worker_stores_event_id_start_time_and_margins():
    result = process_live_odds.calculate_observation(message(
        pinnacle_event_id=1639701789,
        pinnacle_starts_at="2026-10-30T18:00:00Z",
    ))

    assert result["pinnacle_event_id"] == 1639701789
    assert result["pinnacle_starts_at"] == "2026-10-30T18:00:00Z"
    assert result["pinnacle_margin"] == results_store.overround(1.50, 2.70)
    assert result["betclic_margin"] == results_store.overround(1.55, 2.60)


def test_not_found_row_keeps_pinnacle_side_and_blank_betclic_margin():
    result = process_live_odds.calculate_observation(message(
        status="NOT FOUND",
        pinnacle_event_id=1639701789,
        pinnacle_starts_at="2026-10-30T18:00:00Z",
        betclic_odds_for_pin_p1="",
        betclic_odds_for_pin_p2="",
    ))

    assert result["pinnacle_event_id"] == 1639701789
    assert result["pinnacle_starts_at"] == "2026-10-30T18:00:00Z"
    assert result["pinnacle_margin"] == results_store.overround(1.50, 2.70)
    assert result["betclic_margin"] == ""


def test_raw_record_has_typed_event_id_and_blank_for_older_rows():
    new_row = as_csv_row(process_live_odds.calculate_observation(message(
        pinnacle_event_id=1639701789,
        pinnacle_starts_at="2026-10-30T18:00:00Z",
    )))
    old_row = {
        key: value for key, value in new_row.items()
        if key not in ("pinnacle_event_id", "pinnacle_starts_at",
                       *results_store.MARGIN_FIELDS)
    }

    new = results_store.typed_record(results_store.raw_record(new_row, SESSION, True))
    old = results_store.typed_record(results_store.raw_record(old_row, SESSION, True))

    assert new["pinnacle_event_id"] == 1639701789
    assert new["pinnacle_starts_at"] == "2026-10-30T18:00:00Z"
    assert old["pinnacle_event_id"] is None
    assert old["pinnacle_starts_at"] is None
    assert old["pinnacle_margin"] == new["pinnacle_margin"]
    assert old["betclic_margin"] == new["betclic_margin"]


def test_historical_csv_gains_margins_without_changing_ev(tmp_path):
    path = tmp_path / "processed_odds.csv"
    new_columns = ("pinnacle_event_id", "pinnacle_starts_at",
                   *results_store.MARGIN_FIELDS)
    old_header = [
        column for column in process_live_odds.OUTPUT_FIELDS
        if column not in new_columns
    ]
    row = as_csv_row(process_live_odds.calculate_observation(message()))
    not_found = {
        "observation_id": "obs9", "status": "NOT FOUND",
        "pinnacle_odds1": "1.5", "pinnacle_odds2": "2.7",
    }

    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=old_header, extrasaction="ignore")
        writer.writeheader()
        writer.writerows([row, not_found])

    earlier_backup = tmp_path / "processed_odds.legacy_backup.csv"
    earlier_backup.write_text("from an earlier upgrade", encoding="utf-8")
    original = path.read_bytes()

    assert process_live_odds.upgrade_historical_csv(path) is True
    assert process_live_odds.upgrade_historical_csv(path) is False

    # The earlier backup survives and this upgrade got its own.
    assert earlier_backup.read_text(encoding="utf-8") == "from an earlier upgrade"
    backups = list(tmp_path.glob("processed_odds.backup_*.csv"))
    assert [backup.read_bytes() for backup in backups] == [original]

    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        assert reader.fieldnames == process_live_odds.OUTPUT_FIELDS
        upgraded, missing = list(reader)

    for column in old_header:
        assert upgraded[column] == row[column]
    assert float(upgraded["pinnacle_margin"]) == results_store.overround(1.50, 2.70)
    assert float(upgraded["betclic_margin"]) == results_store.overround(1.55, 2.60)
    assert upgraded["pinnacle_event_id"] == ""
    assert float(missing["pinnacle_margin"]) == results_store.overround(1.50, 2.70)
    assert missing["betclic_margin"] == ""


# ---------------------------------------------------- optional second book ---


def superbet_row(**overrides) -> dict:
    data = {
        "status": "FOUND",
        "pinnacle_player1": "Alice Alpha",
        "pinnacle_player2": "Bella Beta",
        "pinnacle_tournament": "WTA Test - R1",
        "superbet_player1": "Alice Alpha",
        "superbet_player2": "Bella Beta",
        "superbet_price1": "1.60",
        "superbet_price2": "2.50",
        "superbet_odds1": "1.408",
        "superbet_odds2": "2.2",
        "ev1": "-0.05",
        "ev2": "-0.20",
        "best_ev": "-0.05",
    }
    data.update(overrides)
    return data


def unconfirmed_row(**overrides) -> dict:
    return superbet_row(**{
        "status": "VERIFY",
        "superbet_player1": "Jazmin Ortenzi",
        "superbet_player2": "Elizabeth Mandlik",
        "ev1": "",
        "ev2": "",
        "best_ev": "",
        **overrides,
    })


def write_superbet(directory: Path, session: str, rows: list[dict]) -> None:
    with (directory / f"superbet_{session}.csv").open(
        "w", encoding="utf-8", newline=""
    ) as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def with_superbet(row: dict, **overrides) -> dict:
    found = superbet_row(**overrides)
    return live_monitor.add_superbet(row, {live_monitor.match_key(found): found})


def test_no_superbet_data_leaves_the_table_unchanged():
    rows = sorted(session_rows(), key=live_monitor.sort_key)

    with live_monitor.plain():
        assert (
            live_monitor.table_lines(rows, args(), compact=True)
            == live_monitor.table_lines(rows, args())
        )
        header = live_monitor.table_lines(rows, args())[0]
        legend = "\n".join(live_monitor.legend_lines(args()))

    assert "Supb" not in header and "EV(Sb)" not in header
    assert "Supb" not in legend


def test_compact_table_has_one_ev_per_book_and_fits_the_terminal():
    row = with_superbet(as_csv_row(process_live_odds.calculate_observation(message(
        pinnacle_player1="Francisco Cerundolo / Tomas Martin Etcheverry",
        pinnacle_player2="Austin Krajicek / Nikola Mektic",
        pinnacle_tournament="ITF Women New Braunfels - R16",
        confidence="manual_review",
    ))), pinnacle_player1="Francisco Cerundolo / Tomas Martin Etcheverry",
        pinnacle_player2="Austin Krajicek / Nikola Mektic",
        pinnacle_tournament="ITF Women New Braunfels - R16",
        superbet_odds1="2.2748", superbet_odds2="1.4243",
        ev1="0.3", ev2="0.3", best_ev="0.3")

    with live_monitor.plain():
        header, rule, line = live_monitor.table_lines([row], args(), compact=True)

    assert " 2.27 " in line

    for label in ("Betc", "Supb", "Mb", "Ms", "EV(Bc)", "EV(Sb)"):
        assert label in header
    for label in ("EV(OR)", "EV(shin)", "EV(prop)"):
        assert label not in header

    assert max(map(len, (header, rule, line))) < 156

    def bars(text, bar="|"):
        return [position for position, char in enumerate(text) if char == bar]

    assert bars(line) == bars(header) == bars(rule, "+")


def test_net_margin_handles_a_price_below_one():
    assert live_monitor.format_net_margin("0.968", "6.16") == "19.54%"
    assert live_monitor.format_net_margin("", "6.16") == "--"


def test_saved_table_with_superbet_keeps_every_method():
    row = with_superbet(session_rows()[0])

    with live_monitor.plain():
        header = live_monitor.table_lines([row], args())[0]

    for label in ("EV(Bc)", "EV(OR)", "EV(shin)", "EV(prop)", "EV(Sb)"):
        assert label in header


def test_bet_on_side_is_the_best_of_either_book():
    row = {"ev1": "-0.10", "ev2": "-0.05"}

    assert live_monitor.table_side(row) == 2
    assert live_monitor.table_side({**row, "superbet_ev1": "0.03", "superbet_ev2": "-0.2"}) == 1
    assert live_monitor.table_side({**row, "superbet_ev1": "-0.2", "superbet_ev2": "-0.3"}) == 2
    assert live_monitor.table_side({"ev1": "", "ev2": "", "superbet_ev1": "-0.2", "superbet_ev2": "-0.1"}) == 2


def test_rows_sort_on_the_best_ev_of_either_book():
    betclic = {"best_ev": "0.01"}
    superbet = {"best_ev": "-0.05", "superbet_best_ev": "0.04"}

    assert sorted([betclic, superbet], key=live_monitor.sort_key)[0] is superbet


def test_read_session_rows_joins_found_and_verify_superbet_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(live_monitor, "SUPERBET_RAW_DIR", tmp_path)
    rows = session_rows()
    path = tmp_path / f"{SESSION}.csv"
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    assert not live_monitor.has_superbet(live_monitor.read_session_rows(path))

    write_superbet(tmp_path, SESSION, [
        superbet_row(),
        unconfirmed_row(pinnacle_player1="Cara Gamma", pinnacle_player2="Dana Delta"),
    ])
    joined = live_monitor.read_session_rows(path)

    assert joined[0]["superbet_odds1"] == "1.408"
    assert joined[0]["superbet_ev1"] == "-0.05"
    assert joined[0]["superbet_status"] == "FOUND"
    assert joined[1]["superbet_odds1"] == "1.408"
    assert joined[1]["superbet_status"] == "VERIFY"
    assert joined[1]["superbet_best_ev"] == ""


def test_read_session_rows_leaves_out_not_found_superbet_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(live_monitor, "SUPERBET_RAW_DIR", tmp_path)
    write_superbet(tmp_path, SESSION, [
        superbet_row(),
        superbet_row(status="NOT FOUND", pinnacle_player1="Cara Gamma", pinnacle_player2="Dana Delta"),
    ])

    assert list(live_monitor.read_superbet(SESSION)) == [
        ("Alice Alpha", "Bella Beta", "WTA Test - R1"),
    ]


def test_unconfirmed_superbet_match_is_marked_and_not_counted():
    row = session_rows()[0]
    unconfirmed = live_monitor.add_superbet(
        row, {live_monitor.match_key(row): unconfirmed_row()}
    )

    with live_monitor.plain():
        header, _, line = live_monitor.table_lines([unconfirmed], args(), compact=True)

    side = live_monitor.table_side(row)
    expected = {1: "1.41?", 2: "2.20?"}[side]
    assert f"|{expected} |" in line
    assert [i for i, c in enumerate(line) if c == "|"] == [i for i, c in enumerate(header) if c == "|"]

    assert live_monitor.table_side(unconfirmed) == live_monitor.table_side(row)
    assert live_monitor.sort_key(unconfirmed) == live_monitor.sort_key(row)

    text = live_monitor.render_report([unconfirmed], args(), START, {}, NOW)
    assert "Superbet match UNCONFIRMED: Jazmin Ortenzi vs Elizabeth Mandlik" in text


def test_unconfirmed_odds_cell_keeps_the_column_width():
    row = {"superbet_status": "VERIFY", "superbet_odds1": "12.345", "superbet_odds2": "100.5"}

    with live_monitor.plain():
        assert live_monitor.superbet_odds_cell(row, 1) == "12.3?"
        assert live_monitor.superbet_odds_cell(row, 2) == " 100?"


def test_report_shows_superbet_prices_as_shown_and_after_tax():
    row = with_superbet(session_rows()[0])

    text = live_monitor.render_report([row], args(), START, {}, NOW)

    assert "Superbet odds as shown: 1.60 / 2.50" in text
    assert "Superbet odds after tax: 1.408 / 2.20" in text
