"""The dashboard's data layer (scripts/dashboard.py). No Streamlit, no network."""

import os
import sys
from types import SimpleNamespace
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import dashboard  # noqa: E402
import process_live_odds  # noqa: E402

NOW = datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)


def row(**overrides) -> dict:
    data = {
        "observation_id": "1",
        "pinnacle_player1": "Ann Alpha",
        "pinnacle_player2": "Bea Beta",
        "pinnacle_tournament": "WTA Chennai - R16",
        "pinnacle_starts_at": "2026-11-03T20:00:00Z",
        "pinnacle_captured_at": "2026-11-03T17:50:00+00:00",
        "betclic_captured_at": "2026-11-03T17:50:10+00:00",
        "pinnacle_odds1": "2.0",
        "pinnacle_odds2": "1.9",
        "betclic_odds_for_pin_p1": "1.8",
        "betclic_odds_for_pin_p2": "2.2",
        "probability1": "0.48",
        "probability2": "0.52",
        "ev1": "-0.136",
        "ev2": "0.144",
        "best_ev": "0.144",
        "status": "FOUND",
        "confidence": "exact",
        "betclic_status": "not_marked_live",
        "observation_gap_seconds": "10",
    }

    return {**data, **overrides}


NO_EV = {"ev1": "", "ev2": "", "best_ev": "", "probability1": "", "probability2": ""}


@pytest.mark.parametrize(
    "tournament, tour, draw",
    [
        ("ATP Paris - R1", "ATP", "Main"),
        ("ATP Paris - Doubles", "ATP", "Doubles"),
        ("ATP Challenger Taipei - Qualifiers", "Challenger", "Qualifiers"),
        ("WTA 125K Cali - Final", "WTA 125", "Main"),
        ("wta 125k Cali - R1", "WTA 125", "Main"),
        ("WTA Hong Kong - R16", "WTA", "Main"),
        ("ITF Men Valencia - R1", "ITF Men", "Main"),
        ("ITF Women Liberec - R1", "ITF Women", "Main"),
        ("Laver Cup", "Other", "Main"),
        ("", "Other", "Main"),
    ],
)
def test_tour_and_draw(tournament, tour, draw):
    assert dashboard.tour_of(tournament) == tour
    assert dashboard.draw_of(tournament) == draw


def test_list_sessions_is_newest_first_and_includes_a_run_without_rows(tmp_path):
    for name in (
        "session_20261103_100000_aaaaaa.csv",
        "session_20261103_100000_aaaaaa_status.json",
        "session_20261103_100000_aaaaaa.txt",
        "session_20261103_120000_bbbbbb_status.json",
        "session_20261102_090000_cccccc.csv",
        "session_notes.csv",
    ):
        (tmp_path / name).touch()

    assert dashboard.list_sessions(tmp_path) == [
        "session_20261103_120000_bbbbbb",
        "session_20261103_100000_aaaaaa",
        "session_20261102_090000_cccccc",
    ]


OLD = "session_20261103_100000_aaaaaa"
NEW = "session_20261103_120000_bbbbbb"


@pytest.mark.parametrize(
    "selected, with_rows, running, picked",
    [
        # A followed run with no row yet: its header, the previous run's table.
        (dashboard.LATEST, {OLD}, {NEW}, (NEW, OLD)),
        (dashboard.LATEST, {OLD, NEW}, {NEW}, (NEW, NEW)),
        # It ended without a row: show that, not an old table.
        (dashboard.LATEST, {OLD}, set(), (NEW, NEW)),
        (dashboard.LATEST, set(), {NEW}, (NEW, NEW)),
        # A session the user chose is never replaced.
        (OLD, {OLD, NEW}, {NEW}, (OLD, OLD)),
        (NEW, {OLD}, {NEW}, (NEW, NEW)),
    ],
)
def test_pick_sessions(selected, with_rows, running, picked):
    assert (
        dashboard.pick_sessions(
            selected, [NEW, OLD], with_rows.__contains__, running.__contains__
        )
        == picked
    )


def test_pick_sessions_without_any():
    assert dashboard.pick_sessions(dashboard.LATEST, [], bool, bool) == (None, None)


def test_record_is_for_the_bet_on_side():
    record = dashboard.table_record(row())

    assert record["Bet on"] == "Bea Beta"
    assert record["Opponent"] == "Ann Alpha"
    assert record["Betclic"] == 2.2
    assert record["Pinnacle"] == 1.9
    assert record["Fair prob"] == 0.52
    assert record["Best EV"] == record["EV Betclic"] == 0.144


def test_record_takes_the_better_book_for_the_side():
    record = dashboard.table_record(
        row(
            superbet_ev1="0.30",
            superbet_ev2="-0.1",
            superbet_odds1="2.5",
            superbet_price1="2.84",
        )
    )

    assert record["Bet on"] == "Ann Alpha"
    assert record["EV Betclic"] == -0.136
    assert record["Best EV"] == record["EV Superbet"] == 0.30
    assert record["Superbet net"] == 2.5
    assert record["Superbet"] == 2.84


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "NOT FOUND"},
        {"status": "VERIFY"},
        {"status": "AMBIGUOUS"},
        {"status": "ERROR"},
        {"betclic_status": "live"},
        {},
    ],
)
def test_record_without_ev_has_no_side_values(overrides):
    record = dashboard.table_record(row(**NO_EV, **overrides))

    assert record["Bet on"] == "Ann Alpha"
    assert record["Best EV"] is None
    assert record["Betclic"] is None


def test_records_are_latest_per_match_best_first():
    records = dashboard.table_records(
        [
            row(
                **NO_EV,
                pinnacle_player1="No",
                pinnacle_player2="Odds",
                status="NOT FOUND",
            ),
            row(pinnacle_player1="Low", ev2="0.01", best_ev="0.01"),
            row(pinnacle_player1="Old", ev2="0.50", best_ev="0.50"),
            row(
                pinnacle_player1="Old",
                ev2="0.20",
                best_ev="0.20",
                betclic_captured_at="2026-11-03T17:55:00+00:00",
            ),
        ]
    )

    assert [record["Opponent"] for record in records] == ["Old", "Low", "Odds"]
    assert records[0]["Best EV"] == 0.20


def test_filters():
    def record(name, best, tour="ATP", draw="Main"):
        return {
            "Bet on": name,
            "Opponent": "Rival",
            "Best EV": best,
            "Tour": tour,
            "Draw": draw,
        }

    records = [
        record("Negative", -0.01),
        record("Zero", 0.0),
        record("Small", 0.01),
        record("Exact", 0.02, tour="WTA"),
        record("None", None, draw="Doubles"),
    ]

    def names(**kwargs):
        return [row["Bet on"] for row in dashboard.filter_records(records, **kwargs)]

    assert len(names()) == 5
    assert names(with_ev=True) == ["Zero", "Small", "Exact"]
    assert names(with_ev=True, min_ev=0.01) == ["Small", "Exact"]
    assert names(with_ev=True, min_ev=0.02) == ["Exact"]
    assert names(with_ev=False, min_ev=0.02) == names()
    assert names(tours=["WTA"]) == ["Exact"]
    assert names(tours=[]) == []
    assert names(draws=["Doubles"]) == ["None"]
    assert names(player=" smALL ") == ["Small"]
    assert len(names(player="rival")) == 5


def test_summary_counts_odds_age_up_to_now_after_the_session_ended():
    info = {
        "status": "finished",
        "session_finished_at": "2026-11-03T17:51:00Z",
        "pinnacle": {"matches": 2},
    }
    summary = dashboard.session_summary([row()], info, 0.02, NOW)

    assert not summary["running"]
    assert summary["processed"] == 1
    assert summary["expected"] == 2
    assert summary["pinnacle_age"] == timedelta(minutes=10).total_seconds()
    assert ("Pinnacle matches processed", "1 / 2") in summary["stats"]
    assert "Unique matches" not in dict(summary["stats"])
    assert "Not found" not in dict(summary["stats"])
    assert dict(summary["stats"])["Found on Betclic"].endswith(" / 2")
    assert dict(summary["stats"])["EV calculated"].endswith(" / 2")


def test_found_on_superbet_sits_under_found_on_betclic():
    rows = [row(superbet_odds1="1.80", superbet_best_ev="0.01")]
    info = {"pinnacle": {"matches": 2}}

    summary = dashboard.session_summary(rows, info, 0.02, NOW)["stats"]
    labels = [label for label, _ in summary]
    stats = dict(summary)

    assert stats["Found on Superbet"] == "1 / 2"
    assert labels[labels.index("Found on Betclic") + 1] == "Found on Superbet"
    assert "Superbet found" not in stats

    without = dict(dashboard.session_summary([row()], info, 0.02, NOW)["stats"])
    assert "Found on Superbet" not in without


def test_found_on_superbet_shows_zero_before_the_first_row(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "SUPERBET_RAW_DIR", tmp_path)
    info = {"session_id": "session_x", "status": "running", "pinnacle": {"matches": 110}}

    assert "Found on Superbet" not in dict(
        dashboard.session_summary([], info, 0.02, NOW)["stats"]
    )

    (tmp_path / "superbet_session_x.csv").write_text("status\n", encoding="utf-8")
    stats = dict(dashboard.session_summary([], info, 0.02, NOW)["stats"])

    assert stats["Found on Superbet"] == "0 / 110"
    assert "Superbet" in dashboard.overview_meters(stats, 110)


def overview_text(rows, info=None, min_ev=0.02) -> str:
    summary = dashboard.session_summary(rows, info or {}, min_ev, NOW)

    class Text(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []

        def handle_data(self, data):
            self.parts.append(data)

    parser = Text()
    parser.feed(dashboard.overview_html(summary))

    return " ".join(" ".join(parser.parts).split())


def session_bar(status="finished") -> str:
    summary = {"status": status}

    return dashboard.session_bar_html(summary, "session_20261105_151211_abcdef")


def test_a_finished_run_with_a_failed_book_is_failed_in_red():
    bar = dashboard.session_bar_html(
        {"status": "finished", "failed_sources": ["Superbet"]}, "session_20261105_151211_abcdef"
    )
    assert ">Failed</span>" in bar and dashboard.STATUS_COLOURS["failed"] in bar
    assert 'title="Superbet failed in this run"' in bar
    assert ">Failed</span>" in session_bar("failed") and ">Running</span>" in session_bar("running")


def test_failed_sources_come_from_the_run_ledger(monkeypatch, tmp_path):
    import json

    import run_ledger

    ledger = tmp_path / "runs.jsonl"
    monkeypatch.setattr(run_ledger, "LEDGER_FILE", ledger)
    assert dashboard.failed_sources("session_a") == []  # no ledger yet

    lines = [
        {"run_id": "session_a", "sources": {"betclic": {"state": "failed"}, "superbet": {"state": "ok"},
                                            "kalshi": {"state": "failed"}}},
        {"run_id": "session_b", "sources": {"betclic": {"state": "partial"}, "superbet": {"state": "ok"}}},
    ]
    ledger.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    assert dashboard.failed_sources("session_a") == ["Betclic"]  # markets do not count
    assert dashboard.failed_sources("session_b") == []  # partial worked
    assert dashboard.failed_sources("session_c") == []


def test_session_bar_shows_the_status_and_run_time():
    bar = session_bar()

    # "Last run overview", the status pill, then the run's time on the same line.
    assert '<span class="overview-title">Last run overview</span><span class="session-status"' in bar
    assert ">Successful</span>" in bar and "Last run on</div>" not in bar
    started = dashboard.session_started("session_20261105_151211_abcdef")
    # The year has its own span, so phones can hide it.
    assert (
        '</span><span class="session-time" title="Last run on">'
        f'<span class="year">{started:%Y-}</span>{started:%m-%d %H:%M}</span>'
    ) in bar


@pytest.mark.parametrize(
    "status, colour",
    [
        ("finished", "#1f9d55"),
        ("failed", "#d64545"),
        ("interrupted", "#c98a00"),
        ("running", "#2a78d6"),
    ],
)
def test_session_bar_colours_the_status(status, colour):
    assert f"--status: {colour}" in session_bar(status)


def test_overview_splits_every_match_by_what_happened_to_it():
    rows = [
        row(observation_id="1"),
        row(pinnacle_player1="Cy Gamma", **{**NO_EV, "betclic_status": "live"}),
        row(pinnacle_player1="Di Delta", **{**NO_EV, "status": "NOT FOUND"}),
        row(pinnacle_player1="Ed Eps", **{**NO_EV, "status": "VERIFY"}),
    ]
    text = overview_text(rows, {"pinnacle": {"matches": 6}})

    assert "Last run overview" in text
    assert "EV calculated 1" in text
    assert "No EV 1" in text
    assert "Found, no EV" not in text
    assert "Not matched 2" in text
    assert "Waiting 2" in text
    assert "Betclic 2 / 6" in text


def test_overview_counts_started_matches_apart_from_waiting(tmp_path, monkeypatch):
    # Pinnacle listed 3: one played (has a row), one already started (skipped by the
    # collectors, so no row ever comes), one prematch still on its way.
    monkeypatch.setattr(dashboard, "PINNACLE_RAW_DIR", tmp_path)
    monkeypatch.setattr(dashboard, "STARTED_PAIRS", {})
    (tmp_path / "pinnacle_session_x.csv").write_text(
        "player1,player2,status\n"
        "Ann Alpha,Bea Beta,prematch\n"
        "Lan Mi,Ana Sofia Sanchez,live\n"
        "Cy Gamma,Di Delta,prematch\n",
        encoding="utf-8",
    )
    info = {"session_id": "session_x", "pinnacle": {"matches": 3}}
    summary = dashboard.session_summary([row()], info, 0.02, NOW)
    text = overview_text([row()], info)

    assert (summary["total"], summary["started"]) == (2, 1)
    assert "Started 1" in text and "Waiting 1" in text
    assert "Pinnacle 1 / 2" in text


def test_overview_hides_waiting_and_superbet_when_they_do_not_apply():
    text = overview_text([row()], {"pinnacle": {"matches": 1}})

    assert "Waiting" not in text
    assert "Superbet" not in text.replace("-- Superbet", "")
    assert "Pinnacle 1 / 1" in text


def test_overview_shows_superbet_and_the_threshold():
    rows = [row(superbet_odds1="1.80", superbet_best_ev="0.01")]
    text = overview_text(rows, {"pinnacle": {"matches": 2}}, min_ev=0.03)

    assert "Superbet 1 / 2" in text
    assert "Superbet > 0%" in text
    assert "Betclic \u2265 3.00%" in text
    assert "Superbet \u2265 3.00%" in text


def test_overview_counts_superbet_at_the_threshold():
    evs = ("0.01", "0.03", "0.05", "-0.01", "")
    rows = [
        row(pinnacle_player1=f"Player {ev}", superbet_odds1="1.80", superbet_best_ev=ev)
        for ev in evs
    ]
    stats = dict(dashboard.session_summary(rows, {}, 0.03, NOW)["stats"])

    assert stats["Superbet EV(ref) > 0%"] == "3"
    assert stats["Superbet EV(ref) >= 3.00%"] == "2"


def test_overview_splits_superbet_like_betclic():
    priced = dict(superbet_odds1="1.80", superbet_price1="2.05")
    rows = [
        row(superbet_status="FOUND", superbet_best_ev="0.01", **priced),
        # Prices, but an unconfirmed match: not matched, as for Betclic.
        row(pinnacle_player1="Cy Gamma", superbet_status="VERIFY", **priced),
        row(pinnacle_player1="Di Delta", superbet_status="FOUND", **priced),
        row(pinnacle_player1="Ed Eps"),
    ]
    text = overview_text(rows, {"pinnacle": {"matches": 5}})
    status = text[text.index("EV status") :]
    betclic, superbet = status[: status.index("Superbet")], status[status.index("Superbet") :]

    assert "EV calculated 1 No EV 1 Not matched 2 Waiting 1" in superbet
    assert "EV calculated 4 Waiting 1" in betclic and "No EV" not in betclic


def test_overview_title_is_left_to_the_session_heading():
    summary = dashboard.session_summary([row()], {}, 0.02, NOW)
    assert "Last run overview" not in dashboard.overview_html(summary, titled=False)


def test_overview_puts_the_status_cards_in_the_first_row():
    rows = [row(superbet_odds1="1.80", superbet_best_ev="0.01")]
    summary = dashboard.session_summary(rows, {"pinnacle": {"matches": 2}}, 0.02, NOW)
    first, second = dashboard.overview_html(summary).split('<div class="overview-cards">')[1:]

    assert "EV status" in first and 'donut-book">Betclic' in first and 'donut-book">Superbet' in first
    # With signals the card has its Signals heading again; Stale data is in the bar.
    assert 'overview-heading">Signals<' in second and "Betclic &gt; 0%" in second
    assert "Data flow" in second and "Stale data" not in second


def test_overview_with_the_ev_filter_keeps_only_the_positive_ev_card():
    rows = [row(superbet_odds1="1.80", superbet_best_ev="0.01")]
    summary = dashboard.session_summary(rows, {"pinnacle": {"matches": 2}}, 0.02, NOW)
    page = dashboard.overview_html(summary, ev_only=True)

    assert page.count('<div class="overview-cards">') == 1
    assert "Signals" in page
    assert "Data flow" not in page and "EV status" not in page
    assert 'class="overview-card fit"' in page


def test_overview_with_all_sessions_counts_their_stats():
    summary = dashboard.session_summary([row()], {"pinnacle": {"matches": 2}}, 0.02, NOW)
    ev_stats = {"EV(ref) > 0%": "7", "EV(ref) >= 2.00%": "3"}
    page = dashboard.overview_html(summary, ev_only=True, ev_stats=ev_stats)

    assert "all sessions" in page
    assert ">7<" in page and ">3<" in page


def ev_record(name, best, session, betclic=None, superbet=None, opponent="Rival"):
    return {
        "Bet on": name,
        "Opponent": opponent,
        "Session": session,
        "Best EV": best,
        "EV Betclic": best if betclic is None else betclic,
        "EV Superbet": superbet,
        "Tournament": "WTA Chennai - R16",
    }


def test_latest_ev_matches_takes_each_match_from_its_newest_qualifying_session():
    records = [
        ev_record("Ann", -0.01, "newest"),
        ev_record("Ann", 0.03, "middle"),
        ev_record("Rival", 0.05, "oldest", opponent="Ann"),  # same match, other side
        ev_record("Bea", 0.01, "newest"),
        ev_record("Cat", None, "newest"),
    ]

    matches = dashboard.latest_ev_matches(records, 0.0)

    assert [(r["Bet on"], r["Session"]) for r in matches] == [("Ann", "middle"), ("Bea", "newest")]
    assert dashboard.latest_ev_matches(records, 0.02) == [records[1]]


def test_player_search_over_all_sessions_keeps_every_appearance():
    records = [
        ev_record("Ann", -0.01, "newest"),
        ev_record("Ann", 0.03, "middle"),
        ev_record("Rival", 0.05, "oldest", opponent="Ann"),
        ev_record("Bea", 0.01, "newest"),
    ]
    filters = {"player_all": True, "player": "ann", "min_ev": 0.0}

    candidates = dashboard.table_candidates([], records, filters)
    shown = dashboard.filter_records(candidates, player=filters["player"])

    assert [r["Session"] for r in shown] == ["newest", "middle", "oldest"]
    # Without it, all sessions means the EV filter's one row per match.
    filters["player_all"] = False
    assert dashboard.table_candidates([], records, filters) == dashboard.latest_ev_matches(
        records, 0.0
    )
    assert dashboard.table_candidates(records[:1], None, filters) == records[:1]


def test_positive_ev_stats_count_each_match_once_per_book():
    records = [
        ev_record("Ann", 0.03, "newest"),
        ev_record("Ann", 0.01, "older"),
        ev_record("Bea", 0.01, "newest", superbet=0.02),
        ev_record("Cat", 0.0, "newest"),
    ]

    assert dashboard.positive_ev_stats(records, 0.02) == {
        "EV(ref) > 0%": "2",
        "EV(ref) >= 2.00%": "1",
        "Superbet EV(ref) > 0%": "1",
        "Superbet EV(ref) >= 2.00%": "1",
    }
    assert "Superbet EV(ref) > 0%" not in dashboard.positive_ev_stats(records[:1], 0.02)


def outcome_sources(winner="Bea Beta", loser="Ann Alpha", result_type="completed"):
    import evaluate_ev
    import match_results

    def sighting(captured_at, odds1, odds2):
        return {
            "captured_at": captured_at,
            "player1": "Ann Alpha",
            "player2": "Bea Beta",
            "tournament": "WTA Chennai - R16",
            "starts_at": "2026-11-03T20:00:00Z",
            "odds1": odds1,
            "odds2": odds2,
        }

    results = [
        {
            "date": "2026-11-03",
            "tournament": "Chennai",
            "winner": winner,
            "loser": loser,
            "result_type": result_type,
        }
    ]

    return {
        "sightings": {
            evaluate_ev.match_key("Bea Beta", "Ann Alpha", "WTA Chennai - R16"): [
                sighting("2026-11-03T17:50:00+00:00", "2.0", "1.9"),
                sighting("2026-11-03T19:00:00+00:00", "2.5", "1.55"),
                sighting("2026-11-03T21:00:00+00:00", "9.0", "1.05"),  # after the start
            ]
        },
        "by_day": match_results.index_results(results),
        "covered": date(2026, 11, 4),
    }


AFTER_START = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)


def test_outcome_columns_follow_the_names_and_judge_the_bet_on_side():
    records = dashboard.table_records([row()])  # bets Bea Beta at Betclic 2.2
    out = dashboard.add_outcomes(records, outcome_sources(), AFTER_START)
    record = out[0]

    assert list(record)[:4] == ["Bet on", "Opponent", "Result", "CLV"]
    assert record["Result"] == "Won"

    from betting.margin import remove_margin

    fair = remove_margin([2.5, 1.55], "power")[1]
    assert record["CLV"] == pytest.approx(2.2 * fair - 1)
    assert "1.55, seen 1.0 h before the start" in record["_title CLV"]
    assert "_price" not in dashboard.visible_columns(list(record), False)


def test_outcomes_say_lost_pending_and_how_the_match_ended():
    records = dashboard.table_records([row()])
    lost = outcome_sources(winner="Ann Alpha", loser="Bea Beta", result_type="retired")

    assert dashboard.add_outcomes(records, lost, AFTER_START)[0]["Result"] == "Lost (retired)"

    before = datetime(2026, 11, 3, 19, 30, tzinfo=timezone.utc)
    early = dashboard.add_outcomes(records, outcome_sources(), before)[0]
    assert early["Result"] == "Pending" and early["CLV"] is None

    unplayed = {**outcome_sources(), "by_day": {}, "covered": date(2026, 11, 3)}
    assert dashboard.add_outcomes(records, unplayed, AFTER_START)[0]["Result"] == "Pending"
    covered = {**unplayed, "covered": date(2026, 11, 10)}
    assert dashboard.add_outcomes(records, covered, AFTER_START)[0]["Result"] == "No result"


def test_outcome_columns_come_one_at_a_time_and_colour_the_result():
    records = dashboard.table_records([row()])
    only_result = dashboard.add_outcomes(records, outcome_sources(), AFTER_START, clv=False)

    assert "Result" in only_result[0] and "CLV" not in only_result[0]
    assert dashboard.result_css("Won (walkover)") == dashboard.EV_GREEN
    assert dashboard.result_css("Lost") == dashboard.EV_RED
    assert dashboard.result_css("Pending") == dashboard.DIMMED

    page = dashboard.table_html(dashboard.add_outcomes(records, outcome_sources(), AFTER_START), 0.02, False)
    assert ">Result<" in page and ">CLV<" in page and "_price" not in page
    assert 'title="Closing Pinnacle 1.55' in page


def test_session_records_are_read_again_only_when_the_file_changes(tmp_path, monkeypatch):
    import csv

    monkeypatch.setattr(dashboard, "SESSIONS_DIR", tmp_path)
    session_id = "session_20261103_170000_abcdef"
    path = tmp_path / f"{session_id}.csv"

    def write(rows):
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    write([row()])
    cache = {}
    first = dashboard.cached_session_records(session_id, cache)

    assert [r["Bet on"] for r in first] == ["Bea Beta"]
    assert list(first[0])[:3] == ["Bet on", "Opponent", "Session"]
    assert first[0]["Session"] == dashboard.session_label(session_id)
    assert dashboard.cached_session_records(session_id, cache) is first

    write([row(), row(pinnacle_player1="Cat Gamma")])
    assert len(dashboard.cached_session_records(session_id, cache)) == 2
    assert dashboard.cached_session_records("session_20261103_180000_abcdef", cache) == []


def test_empty_ev_filter_names_the_best_ev():
    records = [{"Best EV": -0.003}, {"Best EV": None}]
    filters = {"with_ev": True, "min_ev": 0.003}

    assert dashboard.empty_message(records, filters) == (
        "No match in this session has an EV of at least 0.3%. The best is -0.3%."
    )
    assert "none has an EV" in dashboard.empty_message([], filters)
    assert dashboard.empty_message(records, {"with_ev": False}) == "No match fits the filters."


def test_overview_rows_have_the_same_columns():
    with_superbet = [row(superbet_odds1="1.80", superbet_best_ev="0.01")]
    # Both books share one EV status card, so two columns either way.
    for rows, columns in ((with_superbet, 2), ([row()], 2)):
        summary = dashboard.session_summary(rows, {"pinnacle": {"matches": 2}}, 0.02, NOW)
        first, second = dashboard.overview_html(summary).split('<div class="overview-cards">')[1:]

        assert first.count("overview-card wide") == columns
        assert second.count("overview-card wide") == columns


def test_checks_card_shows_the_gap_of_each_book_to_pinnacle():
    rows = [
        row(
            pinnacle_player1=f"Ann {seconds}",
            betclic_captured_at=f"2026-11-03T17:50:{seconds}+00:00",
            superbet_captured_at="2026-11-03T17:50:02+00:00",
            superbet_odds1="1.80",
            superbet_best_ev="0.01",
        )
        for seconds in (10, 13, 40)
    ]
    text = overview_text(rows)

    # In the order the prices came, each arrow the time since the box before it.
    assert "Pinnacle 10 min 00 s ago +2.0 s Superbet +11.0 s Betclic" in text


def test_flow_puts_a_book_without_data_last():
    assert "Pinnacle 10 min 00 s ago +10.0 s Betclic -- Superbet" in overview_text([row()])


def test_flow_shows_the_gap_in_minutes_past_a_minute():
    rows = [row(betclic_captured_at="2026-11-03T17:51:05+00:00")]

    assert "+1m05s Betclic" in overview_text(rows)


def test_pinnacle_box_has_no_tip_without_a_pinnfeed_source():
    flow = dashboard.overview_flow({"pinnacle_age": 60.0, "pinnacle_captured": 1.0, "betclic_gap": 5.0})
    assert '<div class="flow-node"><div class="flow-name">Pinnacle</div>' in flow
    assert "has-tip" not in flow and "Pinnacle data age" not in flow
    assert "Data flow and age" in overview_text([row()])


def test_pinnacle_box_tip_names_pinnfeed_its_quota_and_delay(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "PINNACLE_RAW_DIR", tmp_path)
    for name in ("pinnacle_session_20261119_235502_aaaaaa.csv",  # the day before
                 "pinnacle_session_20261120_000002_bbbbbb.csv",
                 "pinnacle_session_20261120_125502_f402f8.csv",
                 "pinnacle_session_20261120_130002_cccccc.csv"):  # after this run
        (tmp_path / name).write_text("", encoding="utf-8")
    info = {
        "session_id": "session_20261120_125502_f402f8",
        "pinnacle": {"source": "pinnfeed", "quota_remaining": "957", "quota_limit": "1000",
                     "delay_seconds": "30"},
    }

    source = dashboard.pinnacle_source(info)
    assert source == {"tennis_today": 2, "used": 43, "limit": 1000, "delay": 30}
    finished = {"session_id": info["session_id"], "status": "finished",
                "performance": {"pinnacle": info["pinnacle"]}}
    assert dashboard.pinnacle_source(finished) == source

    flow = dashboard.overview_flow({"pinnacle_age": 60.0, "pinnacle_captured": 1.0,
                                    "pinnacle_source": source})
    assert '<div class="flow-node has-tip" tabindex="0">' in flow
    assert 'Source: <a href="https://pinnfeed.io">PinnFeed</a>' in flow
    assert "Today: 2 / 288 (1,000 free)" in flow
    assert "Account" not in flow
    assert "Delay: 30 s" in flow


def test_pinnacle_source_falls_back_to_the_free_plan_and_skips_older_runs():
    assert dashboard.pinnacle_source({"pinnacle": {"source": "pinnapi"}}) is None
    assert dashboard.pinnacle_source({}) is None

    source = dashboard.pinnacle_source({"session_id": "x", "pinnacle": {"source": "pinnfeed"}})
    assert source == {"tennis_today": None, "used": None, "limit": 1000, "delay": 30}
    tip = dashboard.pinnacle_tip(source)
    assert "Today" not in tip and "Delay: 30 s" in tip


def test_flow_arrows_give_the_total_since_pinnacle_on_hover():
    summary = {"pinnacle_age": 600.0, "pinnacle_captured": 1.0, "betclic_gap": 13.0, "superbet_gap": 2.0}
    flow = dashboard.overview_flow(summary)

    assert "capture, per match: 13.0 s" in flow and "capture, per match: 2.0 s" in flow
    assert flow.index("Superbet") < flow.index("Betclic")


def test_session_bar_shows_the_start_to_the_minute_without_the_pinnacle_age():
    summary = dashboard.session_summary([row()], {}, 0.02, NOW)
    summary["status"] = "finished"
    bar = dashboard.session_bar_html(summary, "session_20261105_151211_abcdef")
    started = dashboard.session_started("session_20261105_151211_abcdef")

    assert f"</span>{started:%m-%d %H:%M}</span>" in bar
    assert f"{started:%H:%M:%S}" not in bar
    assert " ago" not in bar and "data-since" not in bar


def test_checks_card_drops_the_seconds_once_the_data_is_stale():
    assert dashboard.pinnacle_age_text(14 * 60 + 5) == "14 min 05 s"
    assert dashboard.pinnacle_age_text(15 * 60) == "15 min 00 s"
    assert dashboard.pinnacle_age_text(15 * 60 + 1) == "15 min"
    assert dashboard.pinnacle_age_text(59 * 60 + 59) == "59 min"
    assert dashboard.pinnacle_age_text(3600 + 2 * 60) == "1 h 02 min"


def test_flow_gives_the_browser_the_capture_time_to_count_from():
    summary = dashboard.session_summary([row()], {}, 0.02, NOW)
    summary["status"] = "finished"
    captured = (NOW - timedelta(minutes=10)).timestamp()

    assert summary["pinnacle_captured"] == captured
    assert f'data-since="{captured}" data-stale-after="900"' in dashboard.overview_html(summary)


def test_pill_turns_stale_once_the_pinnacle_odds_pass_the_limit():
    at_limit = "2026-11-03T17:45:00+00:00"
    past_limit = "2026-11-03T17:44:59+00:00"

    def bar(captured_at, **extra):
        summary = dashboard.session_summary([row(pinnacle_captured_at=captured_at)], {}, 0.02, NOW)
        summary.update(status="finished", **extra)
        return dashboard.session_bar_html(summary, "session_20261105_151211_abcdef")

    fresh = bar(at_limit)
    assert ">Successful</span>" in fresh and "Stale data" not in fresh
    # The browser turns it Stale at the limit, without waiting for the server.
    limit = dashboard.parse_timestamp(at_limit).timestamp() + 900
    assert f'data-stale-at="{limit:.0f}"' in fresh  # whole seconds, not 1.79e+09

    stale = bar(past_limit)
    assert ">Stale</span>" in stale and dashboard.STATUS_COLOURS["stale"] in stale
    assert 'title="Pinnacle odds over 15 min old: no run since this one"' in stale
    # A failure wins over staleness.
    failed = bar(past_limit, failed_sources=["Betclic"])
    assert ">Failed</span>" in failed and "data-stale-at" not in failed


def test_a_later_run_that_failed_without_a_session_makes_the_pill_failed(monkeypatch, tmp_path):
    import json

    import run_ledger

    ledger = tmp_path / "runs.jsonl"
    monkeypatch.setattr(run_ledger, "LEDGER_FILE", ledger)
    shown = "session_20261105_151211_abcdef"

    def write(record):
        ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")

    write({"run_id": shown, "kind": "session", "started_at": "2026-11-05T15:12:11Z",
           "status": "finished", "sources": {"pinnacle": {"state": "ok"}}})
    assert dashboard.later_failure(shown) is None

    write({"run_id": "books_20261105_151700_aaaaaa", "kind": "books",
           "started_at": "2026-11-05T15:17:00Z", "status": "failed",
           "sources": {"pinnacle": {"state": "failed", "detail": "RuntimeError: No matches parsed"}}})
    text = dashboard.later_failure(shown)
    assert text.startswith("The run at ") and text.endswith("failed: Pinnacle RuntimeError: No matches parsed")

    # Skipped (lock held) is not a failure.
    write({"run_id": "", "kind": "skipped", "started_at": "2026-11-05T15:17:00Z",
           "status": "skipped", "sources": {}})
    assert dashboard.later_failure(shown) is None

    summary = {"status": "finished", "later_failure": "The run at 16:17 failed"}
    assert ">Failed</span>" in dashboard.session_bar_html(summary, shown)


def test_checks_card_without_prices():
    text = overview_text([])

    assert "Pinnacle -- -- Betclic -- Superbet" in text
    assert "Stale data" not in text

    # With no signal: no heading, "None" over a "Signals" label.
    page = dashboard.overview_html(dashboard.session_summary([], {}, 0.02, NOW))
    assert 'overview-heading">Signals<' not in page
    assert '<div class="value overview-empty">None</div><div class="label">Signals</div>' in page


def test_overview_of_a_run_without_rows():
    assert "Waiting 3" in overview_text([], {"pinnacle": {"matches": 3}})
    assert "EV calculated 0" in overview_text([])


def test_overview_marks_a_problem_in_amber_without_a_sign():
    rows = [row(**{**NO_EV, "status": "ERROR"})]
    overview = dashboard.overview_html(dashboard.session_summary(rows, {}, 0.02, NOW))

    assert "1 Errors" in overview_text(rows)
    assert f'style="{dashboard.EV_AMBER}">1<' in overview
    assert "\u26a0" not in overview


def test_summary_of_a_run_without_rows():
    summary = dashboard.session_summary(
        [], {"status": "running", "session_start": "2026-11-03T17:59:00Z"}, 0.02, NOW
    )

    assert summary["running"]
    assert summary["pinnacle_age"] is None
    assert summary["idle"] == 60


@pytest.mark.parametrize(
    "value, css",
    [
        (-0.01, dashboard.EV_RED),
        (0.0, dashboard.EV_GRAY),
        (0.01, dashboard.EV_AMBER),
        (0.02, dashboard.EV_GREEN),
        (float("nan"), ""),
        (None, ""),
    ],
)
def test_ev_colours_use_the_monitor_thresholds(value, css):
    assert dashboard.ev_css(value, 0.02) == css


def test_launch_runs_streamlit_on_itself_from_the_repo_root(monkeypatch):
    calls = []
    monkeypatch.setattr(
        dashboard.subprocess,
        "call",
        lambda command, cwd: calls.append((command, cwd)) or 0,
    )

    assert dashboard.launch() == 0

    command, cwd = calls[0]
    assert command[1:4] == ["-m", "streamlit", "run"]
    assert Path(command[4]) == ROOT / "scripts" / "dashboard.py"
    assert cwd == ROOT


def polls_until_stop(counts: list[int]) -> int:
    """How many polls of the tab count it takes. Running out of counts raises
    StopIteration, which means it never decided to stop."""
    remaining = iter(counts)
    dashboard.wait_for_browsers_to_close(lambda: next(remaining), sleep=lambda _: None)

    return len(counts) - sum(1 for _ in remaining)


def test_keep_running_turns_the_browser_watcher_off(monkeypatch):
    started = []
    monkeypatch.setattr(
        dashboard.threading,
        "Thread",
        lambda **kw: started.append(kw) or SimpleNamespace(start=lambda: None),
    )

    monkeypatch.setenv(dashboard.KEEP_RUNNING_ENV, "1")
    dashboard.start_browser_watch()
    assert started == []

    monkeypatch.setenv(dashboard.KEEP_RUNNING_ENV, "0")
    dashboard.start_browser_watch()
    assert len(started) == 1


GRACE = dashboard.STOP_AFTER_SECONDS


def test_stops_after_the_last_tab_has_been_gone_long_enough():
    assert polls_until_stop([1, 2, 1] + [0] * GRACE + [0, 0]) == 3 + GRACE


def test_a_page_reload_restarts_the_wait():
    gap = [0] * (GRACE - 1)

    assert polls_until_stop([1] + gap + [1] + [0] * GRACE) == 2 + len(gap) + GRACE


def test_keeps_waiting_for_the_first_tab():
    with pytest.raises(StopIteration):
        polls_until_stop([0] * (GRACE * 5))


def table_cells(records: list[dict], superbet: bool = False) -> tuple[list[str], list[dict]]:
    """Header names, and per row a dict of column -> (text, attributes)."""

    class Parser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.heads: list[str] = []
            self.rows: list[list] = []
            self.attrs: dict | None = None
            self.text = ""
            self.in_body = False

        def handle_starttag(self, tag, attrs):
            if tag == "tbody":
                self.in_body = True
            if tag == "tr" and self.in_body:
                self.rows.append([])
            if tag in ("th", "td"):
                self.attrs, self.text = dict(attrs), ""

        def handle_data(self, data):
            if self.attrs is not None:
                self.text += data

        def handle_endtag(self, tag):
            if tag == "th":
                self.heads.append(self.text)
            if tag == "td":
                self.rows[-1].append((self.text, self.attrs))
            if tag in ("th", "td"):
                self.attrs = None

    parser = Parser()
    parser.feed(dashboard.table_html(records, 0.02, superbet=superbet))

    return parser.heads, [dict(zip(parser.heads, cells)) for cells in parser.rows]


def test_table_handles_rows_without_start_or_superbet():
    records = dashboard.table_records(
        [
            row(pinnacle_starts_at=""),
            row(**NO_EV, pinnacle_player1="Cara", status="NOT FOUND"),
        ]
    )
    heads, rows = table_cells(records)

    assert "EV Superbet" not in heads
    assert "Tour" not in heads
    assert "Best EV" not in heads
    assert "Book" not in heads
    assert rows[1]["EV Betclic"][0] == "--"
    assert [cells["Starts"][0] == "--" for cells in rows].count(True) == 1


def test_superbet_prices_share_a_switch_and_only_sessions_with_superbet_have_it():
    records = dashboard.table_records(
        [row(superbet_ev2="0.30", superbet_odds2="2.5", superbet_price2="2.84")]
    )
    heads, rows = table_cells(records, superbet=True)

    assert heads.index("Superbet net") == heads.index("Superbet") + 1
    assert rows[0]["Superbet"][0] == "2.84"
    assert rows[0]["Superbet net"][0] == "2.50"
    assert "superbet-shown" in rows[0]["Superbet"][1]["class"]
    assert "superbet-net" in rows[0]["Superbet net"][1]["class"]
    assert dashboard.table_html(records, 0.02, True).count('class="tax-switch"') == 4
    assert "tax-switch" not in dashboard.table_html(records, 0.02, False)


def test_cards_show_each_match_with_its_books_for_phones():
    records = dashboard.table_records(
        [
            row(superbet_ev2="0.30", superbet_odds2="2.5", superbet_price2="2.84"),
            row(**NO_EV, pinnacle_player1="Cara", status="NOT FOUND"),
        ]
    )
    with_superbet = dashboard.cards_html(records, 0.02, True)
    without = dashboard.cards_html(records, 0.02, False)

    assert with_superbet.count('class="match-card"') == 1
    assert with_superbet.count('class="match-card dimmed"') == 1
    assert ">Superbet<" in with_superbet and ">2.84<" in with_superbet
    assert "net 2.50" in with_superbet
    assert ">Superbet<" not in without and ">Betclic<" in without
    assert ">Betclic<" not in dashboard.cards_html(records, 0.02, True, betclic=False)
    assert "no EV" in with_superbet
    assert "&#x27;" not in with_superbet and 'style="color' in with_superbet


def test_superbet_margin_has_both_tax_versions_and_only_sessions_with_superbet_have_them():
    records = dashboard.table_records(
        [
            row(
                superbet_ev2="0.30",
                superbet_price1="1.60",
                superbet_price2="2.50",
                superbet_odds1="1.408",
                superbet_odds2="2.2",
            )
        ]
    )
    heads, rows = table_cells(records, superbet=True)

    assert heads.index("Margin Superbet net") == heads.index("Margin Superbet") + 1
    assert rows[0]["Margin Superbet"][0] == "2.50%"
    assert rows[0]["Margin Superbet net"][0] == "16.48%"
    assert "data-flip" in rows[0]["Margin Superbet"][1]
    assert "data-flip" in rows[0]["Superbet"][1]
    assert "data-flip" not in rows[0]["Betclic"][1]
    assert "margin-shown" in rows[0]["Margin Superbet"][1]["class"]
    assert "margin-net" in rows[0]["Margin Superbet net"][1]["class"]
    assert "Margin Superbet" not in table_cells(records)[0]
    assert "Margin Superbet net" not in table_cells(records)[0]


SUPERBET_METHODS = dict(
    superbet_ev2="0.30",
    superbet_odds2="2.5",
    superbet_price2="2.84",
    superbet_ev2_odds_ratio="0.31",
    superbet_ev2_shin="-0.02",
    superbet_ev2_proportional="0.01",
)


def test_other_devig_methods_have_one_switch_between_the_books():
    records = dashboard.table_records([row(**SUPERBET_METHODS)])
    heads, rows = table_cells(records, superbet=True)
    html = dashboard.table_html(records, 0.02, True)

    for label in ("EV(OR)", "EV(shin)", "EV(prop)"):
        assert heads.index(f"{label} Superbet") == heads.index(f"{label} Betclic") + 1
        assert "ev-betclic" in rows[0][f"{label} Betclic"][1]["class"]
        assert "ev-superbet" in rows[0][f"{label} Superbet"][1]["class"]

    # In both of the first pair's headers, of which one shows at a time.
    assert html.count('class="book-switch"') == 2
    assert rows[0]["EV(OR) Superbet"][0] == dashboard.format_ev(0.31)


def test_without_superbet_the_other_methods_stay_visible_after_switching():
    """Their class would hide them on a page still switched to Superbet."""
    records = dashboard.table_records([row()])
    heads, _ = table_cells(records)
    html = dashboard.table_html(records, 0.02, False)

    assert "EV(OR) Superbet" not in heads
    assert "EV(OR) Betclic" in heads
    assert "ev-betclic" not in html
    assert "book-switch" not in html


def test_all_ev_columns_are_coloured_by_value():
    records = dashboard.table_records([row(**SUPERBET_METHODS)])
    _, rows = table_cells(records, superbet=True)

    def colour(column: str) -> str:
        return rows[0][column][1].get("style", "")

    assert "#1f9d55" in colour("EV(OR) Superbet")
    assert "#d64545" in colour("EV(shin) Superbet")
    assert "#c98a00" in colour("EV(prop) Superbet")


def test_table_colours():
    records = dashboard.table_records(
        [
            row(),
            row(**NO_EV, pinnacle_player1="Cara", status="NOT FOUND"),
        ]
    )
    _, rows = table_cells(records)

    def colour(position: int, column: str) -> str:
        return rows[position][column][1].get("style", "")

    assert "#0e8fa8" in colour(0, "Bet on")
    assert "#1f9d55" in colour(0, "EV Betclic")
    assert colour(0, "Tournament") == ""
    assert "#888888" in colour(1, "Bet on")
    assert "#888888" in colour(1, "EV Betclic")


def test_table_escapes_names():
    records = dashboard.table_records([row(pinnacle_player1="<b>Ana</b> & Co")])
    _, rows = table_cells(records)
    side = [cells for cells in rows[0].values() if "Ana" in cells[0]][0]

    assert side[0] == "<b>Ana</b> & Co"
    assert "<b>Ana" not in dashboard.table_html(records, 0.02, superbet=False)


@pytest.mark.parametrize(
    "name, short",
    [
        ("Laura Samson / Tamara Zidansek", "L. Samson / T. Zidansek"),
        ("Irene Burillo Escorihuela / Julia Riera", "I. Escorihuela / J. Riera"),
        ("Quentin Halys / Pierre-Hugues Herbert", "Q. Halys / P. Herbert"),
        ("Solo / Julia Riera", "Solo / J. Riera"),
        ("Giovanni Mpetshi Perricard", "Giovanni Mpetshi Perricard"),
        ("", ""),
    ],
)
def test_short_doubles(name, short):
    assert dashboard.short_doubles(name) == short


def test_doubles_are_short_on_screen_but_full_for_search_and_hover():
    full = "Irene Burillo Escorihuela / Julia Riera"
    records = dashboard.table_records([row(pinnacle_player1=full, **NO_EV)])
    _, rows = table_cells(records)

    assert rows[0]["Bet on"][0] == "I. Escorihuela / J. Riera"
    assert rows[0]["Bet on"][1]["title"] == full
    assert "title" not in rows[0]["Opponent"][1]
    assert dashboard.filter_records(records, player="Irene") == records


def test_found_without_ev_is_split_by_reason_from_the_worker_notes():
    def worker_row(number, **message):
        result = process_live_odds.calculate_observation(
            {
                "pinnacle_player1": f"Ann {number}",
                "pinnacle_player2": f"Bea {number}",
                "pinnacle_tournament": "WTA Chennai - R16",
                "status": "FOUND",
                **message,
            }
        )
        return row(**{**NO_EV, **{k: v for k, v in result.items() if v != ""}})

    rows = [
        worker_row(1, odds_ok=True, betclic_status="live"),
        worker_row(2, odds_ok=True, betclic_status="live"),
        worker_row(3, odds_ok=False, betclic_status="live"),
        worker_row(4, odds_ok=False, betclic_status="not_marked_live"),
        worker_row(5, odds_ok=True, betclic_status="not_marked_live"),
        row(pinnacle_player1="Ann 6", note="Calculation error: bad odds", **NO_EV),
        row(pinnacle_player1="Ann 7", **NO_EV),
        row(pinnacle_player1="Ann 8"),
        row(pinnacle_player1="Ann 9", status="NOT FOUND", **NO_EV),
    ]
    summary = dashboard.session_summary(rows, {}, 0.02, NOW)["stats"]
    labels = [label for label, _ in summary]
    stats = dict(summary)
    indent = dashboard.SUBGROUP_INDENT

    assert stats["FOUND but no EV"] == "7"
    assert stats[f"{indent}live"] == "3"
    assert stats[f"{indent}odds missing"] == "2"
    assert stats[f"{indent}calculation error"] == "1"
    assert stats[f"{indent}other"] == "1"
    assert labels[labels.index("FOUND but no EV") + 1] == f"{indent}live"


def test_found_without_ev_has_no_subgroup_at_zero():
    labels = dict(dashboard.session_summary([row()], {}, 0.02, NOW)["stats"])

    assert labels["FOUND but no EV"] == "0"
    assert not any(label.startswith(dashboard.SUBGROUP_INDENT) for label in labels)


def test_errors_are_hidden_for_a_clean_feed_run_only():
    def errors(info, status="FOUND"):
        stats = dashboard.session_summary([row(status=status)], info, 0.02, NOW)["stats"]
        return dict(stats).get("Errors")

    def performance(mode, *phases):
        return {
            "performance": {
                "betclic": {"mode": mode, "phases": [{"name": p} for p in phases]}
            }
        }

    assert errors({}) is None
    assert errors(performance("feed", "feed")) is None
    assert errors(performance("feed", "feed"), status="ERROR") == "1"
    assert errors(performance("feed", "tournaments")) == "0"
    assert errors(performance("search")) == "0"


def test_sessions_by_day_groups_by_local_day_earliest_first():
    # Noon UTC is the same calendar day in any time zone within 11 hours of UTC.
    late = "session_20261103_120500_aaaaaa"
    early = "session_20261103_120000_bbbbbb"
    other = "session_20261101_120000_cccccc"

    days = dashboard.sessions_by_day([late, other, early, "session_notes"])

    assert days == {
        dashboard.date(2026, 11, 3): [early, late],
        dashboard.date(2026, 11, 1): [other],
    }


def test_month_weeks_start_on_monday_and_blank_other_months():
    weeks = dashboard.month_weeks(dashboard.date(2026, 11, 1))

    # 1 Nov 2026 is a Sunday, 30 Nov a Monday.
    assert weeks[0] == [None] * 6 + [dashboard.date(2026, 11, 1)]
    assert weeks[-1] == [dashboard.date(2026, 11, 30)] + [None] * 6
    assert sum(day is not None for week in weeks for day in week) == 30


@pytest.mark.parametrize(
    "first, step, shifted",
    [
        ((2026, 11, 1), 1, (2026, 12, 1)),
        ((2026, 12, 1), 1, (2027, 1, 1)),
        ((2026, 1, 1), -1, (2025, 12, 1)),
        ((2026, 11, 1), -11, (2025, 12, 1)),
    ],
)
def test_shift_month(first, step, shifted):
    assert dashboard.shift_month(dashboard.date(*first), step) == dashboard.date(*shifted)


@pytest.mark.parametrize(
    "status, mark",
    [("finished", ""), ("running", " ●"), ("failed", " ✕"), ("interrupted", " !")],
)
def test_session_button_shows_start_and_status(status, mark):
    session_id = "session_20261103_120511_aaaaaa"
    started = dashboard.session_started(session_id)

    text, tooltip = dashboard.session_button(session_id, status)

    assert text == started.strftime("%H:%M") + mark
    assert tooltip == f"{started.strftime('%H:%M:%S')} · {status}"


class FakeStreamlit:
    def __init__(self, state):
        self.session_state = state


def test_picking_latest_puts_every_setting_back_and_resets_the_browser_switches():
    state = {
        "with_ev": True, "ev_scope": dashboard.SCOPE_ALL, "min_ev": 0.5,
        "show_result": True, "show_clv": True, "tours": ["ATP"], "draws": [],
        "player": "Sinner", "player_all": True, dashboard.SESSION_KEY: "session_20261105_100000_abcdef",
    }

    dashboard.choose_session(FakeStreamlit(state), dashboard.LATEST)

    assert {key: state[key] for key in dashboard.SETTING_DEFAULTS} == dashboard.SETTING_DEFAULTS
    assert state[dashboard.SESSION_KEY] == dashboard.LATEST
    assert state[dashboard.RESET_KEY] == 1
    # A later pick of a pill must not change the defaults themselves.
    state["tours"].append("ATP")
    assert dashboard.SETTING_DEFAULTS["tours"] == [dashboard.ALL_PILL]


def test_all_pill_and_the_others_exclude_each_other():
    ALL = dashboard.ALL_PILL
    choice = dashboard.pill_choice

    assert choice([ALL], [ALL, "ATP"]) == ["ATP"]
    assert choice(["ATP"], ["ATP", "WTA"]) == ["ATP", "WTA"]
    assert choice(["ATP", "WTA"], [ALL, "ATP", "WTA"]) == [ALL]
    assert choice(["ATP"], []) == [ALL]
    assert choice([ALL], []) == [ALL]
    assert choice([ALL], None or []) == [ALL]

    assert dashboard.pill_filter([ALL]) is None
    assert dashboard.pill_filter([]) is None
    assert dashboard.pill_filter(["Main"]) == ["Main"]
    assert dashboard.OTHER_TOUR not in dashboard.TOURS


def test_a_pill_click_is_judged_against_the_selection_before_it():
    ALL = dashboard.ALL_PILL
    state = {"tours": [ALL, "ATP"], "tours_previous": [ALL]}

    dashboard.on_pill_change(state, "tours")

    assert state["tours"] == ["ATP"]


def test_picking_a_past_session_keeps_the_settings():
    state = {"with_ev": True, "player": "Sinner"}

    dashboard.choose_session(FakeStreamlit(state), "session_20261105_100000_abcdef")

    assert state["with_ev"] is True and state["player"] == "Sinner"
    assert dashboard.RESET_KEY not in state


def test_each_reset_sends_a_different_script():
    assert dashboard.reset_script(1) != dashboard.reset_script(2)
    assert "resetMatchesTable" in dashboard.reset_script(1)
    assert "window.resetMatchesTable = " in dashboard.TABLE_SCRIPT


# ------------------------------------------------------------ book switches ---

BOTH_BOOKS = dict(
    superbet_status="FOUND",
    superbet_ev1="0.30",
    superbet_ev2="-0.1",
    superbet_best_ev="0.30",
    superbet_odds1="2.5",
    superbet_odds2="1.4",
    superbet_price1="2.84",
    superbet_price2="1.59",
)


def test_with_superbet_off_bet_on_and_best_ev_come_from_betclic():
    record = dashboard.table_records([row(**BOTH_BOOKS)], ["Betclic"])[0]

    assert record["Bet on"] == "Bea Beta"
    assert record["Best EV"] == record["EV Betclic"] == 0.144
    assert record["EV Superbet"] is None and record["Superbet"] is None
    assert record["_price"] == 2.2


def test_with_betclic_off_bet_on_and_best_ev_come_from_superbet():
    rows = [row(ev1="0.5", best_ev="0.5", **BOTH_BOOKS)]
    record = dashboard.table_records(rows, ["Superbet"])[0]

    assert record["Bet on"] == "Ann Alpha"
    assert record["Best EV"] == record["EV Superbet"] == 0.30
    assert record["EV Betclic"] is None and record["Betclic"] is None
    assert record["_price"] == 2.5
    # Pinnacle's side of the match stays.
    assert record["Pinnacle"] == 2.0 and record["Fair prob"] == 0.48


def test_a_book_turned_off_loses_its_columns():
    rows = [row(**{**BOTH_BOOKS, **SUPERBET_METHODS})]
    betclic = dashboard.table_html(dashboard.table_records(rows, ["Betclic"]), 0.02, False)
    superbet = dashboard.table_html(
        dashboard.table_records(rows, ["Superbet"]), 0.02, True, betclic=False
    )

    assert ">EV Superbet<" not in betclic and ">Margin Superbet" not in betclic
    assert ">EV Betclic<" in betclic and ">Match conf<" in betclic
    for column in ("Betclic", "EV Betclic", "Margin Betclic", "Match conf", "EV(OR) Betclic"):
        assert f">{column}<" not in superbet
    assert ">EV(OR) Superbet<" in superbet and ">EV Superbet<" in superbet
    # One book: no switch, and no class that a page left on the other book would hide.
    assert "book-switch" not in superbet and "ev-superbet" not in superbet


def test_the_books_cache_keeps_each_choice_apart(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(dashboard, "read_session_rows", lambda path: [row(**BOTH_BOOKS)])
    session = "session_20261105_100000_aaaaaa"
    (tmp_path / f"{session}.csv").write_text("x")
    cache = {}

    both = dashboard.cached_session_records(session, cache)
    betclic = dashboard.cached_session_records(session, cache, ["Betclic"])

    assert both[0]["Best EV"] == 0.30
    assert betclic[0]["Best EV"] == 0.144


def overview_for(rows, books, ev_only=False) -> str:
    summary = dashboard.session_summary(rows, {"pinnacle": {"matches": 2}}, 0.02, NOW)
    return dashboard.overview_html(summary, ev_only, books=books)


def test_overview_with_superbet_off_is_betclic_only():
    page = overview_for([row(**BOTH_BOOKS)], ["Betclic"])

    assert "Superbet" not in page
    assert 'donut-book">Betclic' in page and 'flow-name">Betclic' in page


def test_overview_with_betclic_off_is_superbet_only():
    page = overview_for([row(**BOTH_BOOKS, status="ERROR")], ["Superbet"])

    assert "Betclic" not in page
    assert 'donut-book">Superbet' in page and 'flow-name">Superbet' in page
    assert "Superbet &gt; 0%" in page or "Superbet > 0%" in page
    assert ">Errors<" not in page
    first, second = page.split('<div class="overview-cards">')[1:]
    assert "Matches covered" in first and "EV status" in first


def test_overview_ev_card_with_one_book():
    page = overview_for([row(**BOTH_BOOKS)], ["Superbet"], ev_only=True)

    assert "Superbet" in page and "Betclic" not in page


@pytest.mark.parametrize(
    "previous, chosen, expected",
    [
        (["Betclic", "Superbet"], ["Superbet"], ["Superbet"]),
        (["Superbet"], [], ["Superbet"]),
        (["Superbet"], ["Superbet", "Betclic"], ["Betclic", "Superbet"]),
    ],
)
def test_the_last_book_cannot_be_turned_off(previous, chosen, expected):
    assert dashboard.book_choice(previous, chosen) == expected


def test_about_parts_cut_around_image_lines():
    text = "# Title\n\nText.\n\n![Fig](figures/a.png)\n\nMore.\n![B](figures/b.png)\n"
    assert dashboard.about_parts(text) == [
        ("markdown", "# Title\n\nText.\n"),
        ("image", "figures/a.png"),
        ("markdown", "\nMore."),
        ("image", "figures/b.png"),
    ]


def test_every_methodology_figure_exists():
    parts = dashboard.about_parts(dashboard.METHODOLOGY.read_text(encoding="utf-8"))
    images = [value for kind, value in parts if kind == "image"]
    assert images
    for value in images:
        assert (dashboard.METHODOLOGY.parent / value).is_file(), value


# AppTest has no real server, so the browser watcher thread stops at once.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_about_page_renders_without_the_sidebar():
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(Path(dashboard.__file__)), default_timeout=30)
    app.query_params["page"] = dashboard.ABOUT_PAGE
    app.run()
    assert not app.exception
    assert not app.sidebar.children
    assert any("Betting EV" in block.value for block in app.markdown)


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_tennis_is_the_default_tab_with_its_sidebar():
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(Path(dashboard.__file__)), default_timeout=60)
    app.run()
    assert not app.exception
    assert app.sidebar.text_input(key="player")


@pytest.mark.parametrize(
    "captured_at, freshness",
    [("2026-11-03T17:45:00+00:00", "fresh"), ("2026-11-03T17:44:59+00:00", "stale")],
)
def test_pinnacle_age_and_stale_flag_are_green_while_fresh_and_red_once_stale(
    captured_at, freshness
):
    summary = dashboard.session_summary([row(pinnacle_captured_at=captured_at)], {}, 0.02, NOW)
    summary["status"] = "finished"
    overview = dashboard.overview_html(summary)
    assert f'class="flow-age {freshness}"' in overview


def test_calendar_shows_each_days_session_count_in_its_month_only():
    days = {
        date(2026, 11, 2): ["a", "b"],
        date(2026, 11, 9): ["c"] * 96,
        date(2026, 10, 31): ["d"],
    }
    style = dashboard.day_count_style(days, date(2026, 11, 1))

    assert '.st-key-day_2026-11-02 button::after { content: "2"; }' in style
    assert '.st-key-day_2026-11-09 button::after { content: "96"; }' in style
    assert "2026-10-31" not in style
    assert dashboard.day_count_style(days, date(2026, 12, 1)) == ""


# ---------- track record ----------


def track_for(tmp_path, monkeypatch, sessions: dict, min_ev=0.02, books=dashboard.BOOKS):
    """sessions: id -> rows, oldest first, like run order."""
    monkeypatch.setattr(dashboard, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(dashboard, "read_session_rows", lambda path: sessions[path.stem])

    for session_id in sessions:
        (tmp_path / f"{session_id}.csv").write_text("x")

    newest_first = list(reversed(sessions))
    return dashboard.track_record(newest_first, {}, outcome_sources(), min_ev, AFTER_START, books)


def test_track_record_counts_each_match_once_per_book_at_its_first_flag(tmp_path, monkeypatch):
    record = track_for(tmp_path, monkeypatch, {
        "session_20261103_170000_aaaaaa": [row(**BOTH_BOOKS)],
        # Later and lower: the first flag's price is the one counted.
        "session_20261103_180000_bbbbbb": [row(**BOTH_BOOKS, betclic_odds_for_pin_p2="2.0", ev2="0.05",
                                               pinnacle_captured_at="2026-11-03T18:50:00+00:00")],
    })

    betclic, superbet = record["Betclic"], record["Superbet"]
    # Betclic bets Bea Beta at 2.2 (won); Superbet bets Ann Alpha at 2.5 after tax (lost).
    assert (betclic["signals"], betclic["won"], betclic["lost"]) == (1, 1, 0)
    assert betclic["units"] == pytest.approx(1.2) and betclic["ev"] == pytest.approx(0.144)
    assert betclic["expected"] == pytest.approx(1.144 / 2.2) and betclic["clv"] is not None
    assert (superbet["won"], superbet["lost"], superbet["units"]) == (0, 1, -1.0)
    assert betclic["roi_margin"] is None  # one bet has no range


def test_track_record_skips_low_betclic_odds_late_flags_and_books_off(tmp_path, monkeypatch):
    low = row(betclic_odds_for_pin_p2="1.13", ev2="0.05")
    late = row(pinnacle_captured_at="2026-11-03T20:30:00+00:00")
    assert track_for(tmp_path, monkeypatch, {"session_20261103_170000_aaaaaa": [low]}) == {}
    assert track_for(tmp_path, monkeypatch, {"session_20261103_170000_aaaaaa": [late]}) == {}

    # A later flag at a bettable price still counts.
    record = track_for(tmp_path, monkeypatch, {
        "session_20261103_170000_aaaaaa": [low],
        "session_20261103_180000_bbbbbb": [row(betclic_odds_for_pin_p2="1.14", ev2="0.03")],
    })
    assert record["Betclic"]["signals"] == 1 and record["Betclic"]["units"] == pytest.approx(0.14)

    only_betclic = track_for(
        tmp_path, monkeypatch, {"session_20261103_170000_aaaaaa": [row(**BOTH_BOOKS)]},
        books=["Betclic"],
    )
    assert list(only_betclic) == ["Betclic"]


def at(hhmm: str, **overrides) -> dict:
    return row(pinnacle_captured_at=f"2026-11-03T{hhmm}:00+00:00", **overrides)


def test_signal_lasts_until_the_first_run_without_it(tmp_path, monkeypatch):
    record = track_for(tmp_path, monkeypatch, {
        "session_20261103_170000_aaaaaa": [at("17:00")],
        "session_20261103_171500_bbbbbb": [at("17:15")],
        # Still found, but below the minimum: the signal is over.
        "session_20261103_173000_cccccc": [at("17:30", ev2="0.01")],
        "session_20261103_174500_dddddd": [at("17:45")],
    })["Betclic"]

    assert record["lasts"] == pytest.approx(30.0)
    assert (record["lasts_ended"], record["lasts_open"]) == (1, 0)
    assert record["gone_next"] == 0.0 and record["gone_checked"] == 1


def test_signal_gone_at_the_next_run_and_open_ones(tmp_path, monkeypatch):
    other = dict(pinnacle_player1="Cy Gamma", pinnacle_player2="Di Delta")
    record = track_for(tmp_path, monkeypatch, {
        "session_20261103_170000_aaaaaa": [at("17:00"), at("17:00", **other)],
        "session_20261103_171500_bbbbbb": [at("17:15", **other)],
        # The newest run may still be collecting, so Cy Gamma's signal stays open.
        "session_20261103_173000_cccccc": [],
    })["Betclic"]

    assert record["lasts"] == pytest.approx(15.0)
    assert (record["lasts_ended"], record["lasts_open"]) == (1, 1)
    assert record["gone_next"] == 0.5 and record["gone_checked"] == 2


def test_signal_still_bettable_at_the_start_has_not_ended():
    lifetimes = dashboard.signal_lifetimes(
        ["session_20261103_190000_aaaaaa", "session_20261103_201500_bbbbbb", "session_20261103_203000_cccccc"],
        [{"Betclic": dashboard.table_records([at("19:00")])}, {}, {}],
        dashboard.table_records([at("19:00")]),
        "Betclic",
        0.02,
        {},
    )

    assert lifetimes == []  # the next run came 75 minutes later

    lifetimes = dashboard.signal_lifetimes(
        ["session_20261103_194500_aaaaaa", "session_20261103_200000_bbbbbb"],
        [{"Betclic": dashboard.table_records([at("19:45")])}, {}],
        dashboard.table_records([at("19:45")]),
        "Betclic",
        0.02,
        {},
    )
    # Still bettable at the last run before the 20:00 start.
    assert lifetimes == [{"minutes": 0.0, "seen": 0.0, "ended": False, "next_gone": None, "gap": 15.0}]
    assert dashboard.lifetime_stats(lifetimes)["lasts"] is None


def test_track_stats_range_and_open_matches():
    def flag(result, price=2.0, fair=0.5, clv=None):
        return {"Result": result, "_price": price, "Fair prob": fair, "CLV": clv, "Best EV": 0.03}

    stats = dashboard.track_stats([
        flag("Won", clv=0.04), flag("Lost", clv=-0.02), flag("Won", clv=0.01),
        flag("Pending"), flag("No result"), flag("Won (retired)", clv=0.03),
    ])

    assert (stats["signals"], stats["won"], stats["lost"]) == (6, 2, 1)
    assert (stats["pending"], stats["no_result"], stats["other"]) == (1, 1, 1)
    assert stats["roi"] == pytest.approx(1 / 3) and stats["units"] == pytest.approx(1.0)
    assert stats["roi_margin"] == pytest.approx(1.96 * 0.9428090 / 3 ** 0.5, rel=1e-5)
    assert stats["clv_count"] == 4 and stats["clv_positive"] == 0.75


def test_track_card_shows_each_book_and_says_when_nothing_qualified():
    stats = dashboard.track_stats([
        {"Result": "Won", "_price": 2.0, "Fair prob": 0.5, "CLV": 0.05, "Best EV": 0.03},
        {"Result": "Lost", "_price": 2.0, "Fair prob": 0.5, "CLV": -0.01, "Best EV": 0.03},
    ])
    page = dashboard.track_card({"Betclic": stats}, 0.02)

    # No heading: the rules are in the Signals tip and a note under Betclic's tiles.
    assert "Track record (" not in page and "overview-heading" not in page
    assert '<div class="tip">First flag at EV ≥ 2.0%</div>' in page
    assert page.endswith('<div></div><div class="track-note">Odds ≥ 1.14</div></div></div>')
    superbet_only = dashboard.track_card({"Superbet": stats}, 0.02, ["Superbet"])
    assert "track-note" not in superbet_only and "EV ≥ 2.0%" in superbet_only
    assert '<div class="track-book">Betclic</div>' in page
    for text in (">1-1<", ">+2.0%<", ">50%<", "Win rate (exp. 52%)", "ROI ±139%", ">+0.00<"):
        assert text in page
    assert ">Lasts<" in page and "Gone next run" not in page
    assert dashboard.lifetime_text({"lasts": 14.9, "lasts_seen": 0.0}) == "< 15 min"
    assert dashboard.lifetime_text({"lasts": 30.0, "lasts_seen": 15.0}) == "15–30 min"
    assert dashboard.lifetime_text({}) == "--"
    assert dashboard.minutes_text(29.6) == "30 min" and dashboard.minutes_text(150) == "2.5 h"
    assert dashboard.minutes_text(2.52) == "2.5 min" and dashboard.minutes_text(5.01) == "5 min"
    assert "No match has reached this EV" in dashboard.track_card({}, 0.02)

    summary = dashboard.session_summary([row()], {"pinnacle": {"matches": 1}}, 0.02, NOW)
    for ev_only in (False, True):
        overview = dashboard.overview_html(summary, ev_only, track="<p>TRACK</p>")
        assert overview.endswith(
            '<div class="overview-title">Statistics (all runs)</div>'
            '<div class="overview-cards"><p>TRACK</p></div></section>'
        )


def test_settled_outcomes_are_kept_until_the_sources_change(monkeypatch):
    flags = dashboard.table_records([row()])
    sources = {**outcome_sources(), "signature": 1}
    memo, calls = {}, []
    real = dashboard.add_outcomes
    monkeypatch.setattr(dashboard, "add_outcomes", lambda records, *a: calls.append(len(records)) or real(records, *a))

    assert dashboard.settled_outcomes(flags, sources, AFTER_START, memo)[0]["Result"] == "Won"
    assert dashboard.settled_outcomes(flags, sources, AFTER_START, memo)[0]["Result"] == "Won"
    assert calls == [1, 0]

    # Pending is worked out again; new sources drop what was kept.
    before = datetime(2026, 11, 3, 19, tzinfo=timezone.utc)
    dashboard.settled_outcomes(flags, {**sources, "signature": 2}, before, memo)
    dashboard.settled_outcomes(flags, {**sources, "signature": 2}, before, memo)
    assert calls == [1, 0, 1, 1]


def test_track_loader_draws_a_placeholder_until_the_thread_is_done(monkeypatch):
    import threading

    gate = threading.Event()
    calls = []

    def slow(sessions, cache, sources, min_ev, now, books, rechecks=None, longshots=True):
        calls.append((min_ev, books))
        gate.wait(5)
        return {"Betclic": {"signals": 1}}

    monkeypatch.setattr(dashboard, "load_outcome_sources", lambda cache: {})
    monkeypatch.setattr(dashboard, "list_sessions", lambda: [])
    monkeypatch.setattr(dashboard, "track_record", slow)
    loader = dashboard.TrackLoader({}, cache_file=None, results_file=None)

    assert loader.request(0.02, ["Betclic"], wait=0.05) is None  # cold: still loading
    assert not loader.ready(0.02, ["Betclic"])
    assert "Loading" in dashboard.track_card(None, 0.02)
    assert 'class="spinner"' in dashboard.track_card(None, 0.02)
    assert "spinner" not in dashboard.track_card({}, 0.02)

    gate.set()
    loader.thread.join(5)
    assert loader.ready(0.02, ["Betclic"]) and loader.thread is None
    # Held: returned at once (the refresher keeps it current, not the request).
    assert loader.request(0.02, ["Betclic"]) == {"Betclic": {"signals": 1}}
    assert calls == [(0.02, ["Betclic"])]


def test_track_loader_shows_saved_numbers_after_a_restart(monkeypatch, tmp_path):
    import threading

    gate = threading.Event()
    values = iter([{"Betclic": {"signals": 1}}, {"Betclic": {"signals": 2}}])

    def record(*args, **kwargs):
        value = next(values)
        if value["Betclic"]["signals"] == 2:
            gate.wait(5)  # the second server's cold read
        return value

    monkeypatch.setattr(dashboard, "load_outcome_sources", lambda cache: {})
    monkeypatch.setattr(dashboard, "list_sessions", lambda: [])
    monkeypatch.setattr(dashboard, "track_record", record)
    results_file = tmp_path / "track_results.pkl"

    first = dashboard.TrackLoader({}, cache_file=None, results_file=results_file)
    assert first.request(0.02, ["Betclic"], wait=2) == {"Betclic": {"signals": 1}}
    first.thread.join(5)
    assert results_file.exists()

    # A restarted server shows the saved numbers at once, then its refresher's.
    second = dashboard.TrackLoader({}, cache_file=None, results_file=results_file)
    monkeypatch.setattr(second, "inputs_signature", lambda: "new run")
    assert second.request(0.02, ["Betclic"], wait=0.05) == {"Betclic": {"signals": 1}}
    assert second.ready(0.02, ["Betclic"])
    assert second.refresh_once()
    gate.set()
    second.thread.join(5)
    assert second.results[(0.02, ("Betclic",), (0.0, 1.0))] == {"Betclic": {"signals": 2}}
    assert dashboard.load_results(results_file)[0] == {
        (0.02, ("Betclic",), (0.0, 1.0)): {"Betclic": {"signals": 2}}
    }


def test_track_loader_shows_a_failure_on_the_card(monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(dashboard, "load_outcome_sources", lambda cache: {})
    monkeypatch.setattr(dashboard, "list_sessions", lambda: [])
    monkeypatch.setattr(dashboard, "track_record", broken)
    loader = dashboard.TrackLoader({}, cache_file=None, results_file=None)

    assert loader.request(0.02, ["Betclic"], wait=2) is None
    assert loader.ready(0.02, ["Betclic"]) and loader.error == "OSError: disk gone"
    assert "Could not work out the track record: OSError: disk gone" in dashboard.track_card(
        None, 0.02, error=loader.error
    )


def alert(player, book="Betclic", price=3.9, hour=23):
    return {
        "player1": "Ann Alpha",
        "player2": "Bea Beta",
        "tournament": "WTA Chennai - R16",
        "book": book,
        "player": player,
        "at": datetime(2026, 11, 2, hour, 15, tzinfo=timezone.utc),
        "price": price,
        "ev": 0.26 if price else None,
    }


def test_alerted_matches_name_the_first_alert():
    records = dashboard.table_records([row()])  # Bet on Bea Beta

    assert dashboard.add_alerts(records, []) == records
    other_match = {**alert("Bea Beta"), "tournament": "WTA Chennai - QF"}
    assert dashboard.add_alerts(records, [other_match]) == records

    [record] = dashboard.add_alerts(
        records, [alert("Bea Beta", "Superbet", 3.75, hour=23), alert("Bea Beta", price=3.9, hour=22)]
    )
    assert "Alerted" not in record and record["_alert_side"] == "Bet on"
    time = dashboard.alert_time(datetime(2026, 11, 2, 22, 15, tzinfo=timezone.utc))
    assert record["_alert"] == f"🔔 Betclic 3.90, Superbet 3.75 · {time}"
    assert "EV +26.0%" in record["_title Alerted"] and "already bet" in record["_title Alerted"]
    assert dashboard.cell_css("Alerted", record["_alert"], False, 0.02) == dashboard.EV_AMBER

    # An alert on the other player is a different bet, its bell on the Opponent; no
    # price in entries written before prices were kept.
    [record] = dashboard.add_alerts(records, [alert("Ann Alpha", price=None)])
    assert record["_alert"].startswith("🔔 other side: Betclic · ")
    assert record["_alert_side"] == "Opponent"
    assert dashboard.cell_css("Alerted", record["_alert"], False, 0.02) == dashboard.DIMMED
    page = dashboard.table_html([record], 0.02, False)
    assert 'class="alerted-other"' in page and 'class="alerted"' not in page

    # No column: a bell and a tint on the Bet on name, the alert in its tooltip.
    page = dashboard.table_html(dashboard.add_alerts(records, [alert("Bea Beta")]), 0.02, False)
    assert ">Alerted</th>" not in page and 'class="alerted"' in page
    assert '<span class="bell">🔔</span>Bea Beta' in page and "🔔 Betclic 3.90" in page
    cards = dashboard.cards_html(dashboard.add_alerts(records, [alert("Bea Beta")]), 0.02, False)
    assert "🔔 Betclic 3.90" in cards


def test_alerted_rows_get_a_provisional_clv_before_the_start():
    from betting.margin import remove_margin

    records = dashboard.table_records([row()])  # Bet on Bea Beta, starts 20:00 on 3 Nov
    sources = outcome_sources()
    before = datetime(2026, 11, 3, 19, 30, tzinfo=timezone.utc)
    early = dashboard.add_outcomes(records, sources, before)
    assert early[0]["CLV"] is None

    alerts = [alert("Bea Beta", "Superbet", 3.0, hour=23), alert("Bea Beta", price=2.0, hour=22)]
    [record] = dashboard.add_alert_clv(early, alerts, sources, before)
    fair = remove_margin([2.5, 1.55], "power")[1]  # newest snapshot up to now: 19:00
    assert record["CLV"] == pytest.approx(2.0 * fair - 1)  # first alert: Betclic
    assert isinstance(record["CLV"], dashboard.ProvisionalCLV)
    assert dashboard.format_clv(record["CLV"]).startswith("≈")
    assert "0.5 h ago" in record["_title CLV"] and "after tax 2.64" in record["_title CLV"]
    assert "≈" in dashboard.table_html([record], 0.02, False)

    # Started: the real CLV stays; no CLV column or no alert: unchanged.
    started = dashboard.add_outcomes(records, sources, AFTER_START)
    assert dashboard.add_alert_clv(started, alerts, sources, AFTER_START) == started
    assert dashboard.add_alert_clv(records, alerts, sources, before) == records
    assert dashboard.add_alert_clv(early, [], sources, before) == early
    assert not dashboard.format_clv(0.05).startswith("≈")


def test_signals_card_says_none_when_every_count_is_zero():
    stats = {
        "EV(ref) > 0%": "0",
        "EV(ref) >= 2.00%": "0",
        "Superbet EV(ref) > 0%": "0",
        "Superbet EV(ref) >= 2.00%": "0",
    }
    tiles = dashboard.overview_positive(stats)

    assert "None" in tiles and "Betclic" not in tiles
    assert 'class="tip">No match with EV (power method) &gt; 0%<' in tiles

    stats["Superbet EV(ref) > 0%"] = "1"
    tiles = dashboard.overview_positive(stats)

    # Only the book with a signal: Betclic's two zeros are left out.
    assert "None" not in tiles and "Superbet &gt; 0%" in tiles and "Betclic" not in tiles

    stats["EV(ref) > 0%"] = "2"
    tiles = dashboard.overview_positive(stats)
    assert "Betclic &gt; 0%" in tiles and "Superbet ≥ 2.00%" in tiles


def test_compact_sightings_keeps_each_prices_first_and_last_and_the_close():
    import evaluate_ev

    def seen(hhmm, odds1="2.0", starts="2026-11-03T20:00:00Z", player1="Ann Alpha"):
        return {"captured_at": f"2026-11-03T{hhmm}:00+00:00", "player1": player1,
                "odds1": odds1, "odds2": "1.9", "starts_at": starts}

    rows = [seen("17:00"), seen("17:05"), seen("17:10"), seen("17:15"), seen("17:20", "2.1"),
            seen("19:55", "2.1"), seen("20:00", "2.1"), seen("20:05", "2.1"), seen("20:10", "2.1")]
    kept = [row["captured_at"][11:16] for row in evaluate_ev.compact_sightings(rows)]

    # 17:05 and 17:10 go; the last price before the 20:00 start (19:55) stays.
    assert kept == ["17:00", "17:15", "17:20", "19:55", "20:00", "20:10"]
    flipped = [seen("17:00"), seen("17:05", player1="Bea Beta"), seen("17:10")]
    assert evaluate_ev.compact_sightings(flipped) == flipped


def test_outcome_sources_read_each_new_snapshot_once(tmp_path, monkeypatch):
    import match_results

    import evaluate_ev

    header = "captured_at,player1,player2,tournament,starts_at,odds1,odds2\n"

    def snapshot(name, hhmm, odds1):
        path = tmp_path / name
        path.write_text(header + f"2026-11-03T{hhmm}:00+00:00,Ann Alpha,Bea Beta,WTA Chennai - R16,"
                        f"2026-11-03T20:00:00Z,{odds1},1.9\n")
        os.utime(path, (1, 1))

    monkeypatch.setattr(dashboard, "PINNACLE_RAW_DIR", tmp_path)
    monkeypatch.setattr(dashboard, "outcome_signature", lambda: ("results",))
    monkeypatch.setattr(match_results, "load_results", lambda: [])
    monkeypatch.setattr(match_results, "read_covered_through", lambda: None)
    reads = []
    real = evaluate_ev.add_sightings
    monkeypatch.setattr(evaluate_ev, "add_sightings", lambda groups, paths: reads.extend(p.name for p in paths) or real(groups, paths))

    for name, hhmm in (("odds_1.csv", "17:00"), ("odds_2.csv", "17:05"), ("odds_3.csv", "17:10")):
        snapshot(name, hhmm, "2.0")
    cache = {}
    sources = dashboard.load_outcome_sources(cache)
    (key,) = sources["sightings"]
    assert [row["captured_at"][11:16] for row in sources["sightings"][key]] == ["17:00", "17:10"]

    snapshot("odds_4.csv", "17:15", "2.2")
    old = sources["sightings"]
    sources = dashboard.load_outcome_sources(cache)
    assert reads == ["odds_1.csv", "odds_2.csv", "odds_3.csv", "odds_4.csv"]
    assert [row["odds1"] for row in sources["sightings"][key]] == ["2.0", "2.0", "2.2"]
    assert len(old[key]) == 2  # a reader holding the old dict never sees it change


def test_track_flags_survive_a_server_restart(tmp_path, monkeypatch):
    sessions = {"session_20261103_170000_aaaaaa": [row()]}
    monkeypatch.setattr(dashboard, "SESSIONS_DIR", tmp_path)
    (tmp_path / "session_20261103_170000_aaaaaa.csv").write_text("x")
    reads = []
    monkeypatch.setattr(dashboard, "read_session_rows", lambda path: reads.append(path.stem) or sessions[path.stem])
    monkeypatch.setattr(dashboard, "list_sessions", lambda: list(sessions))
    monkeypatch.setattr(dashboard, "load_outcome_sources", lambda cache: outcome_sources())
    cache_file = tmp_path / "cache" / "track_flags.pkl"

    first = dashboard.TrackLoader({}, cache_file=cache_file, results_file=None)
    assert first.request(0.02, ["Betclic"], wait=10)["Betclic"]["signals"] == 1
    assert reads == ["session_20261103_170000_aaaaaa"] and cache_file.exists()

    second = dashboard.TrackLoader({}, cache_file=cache_file, results_file=None)
    assert second.request(0.02, ["Betclic"], wait=10)["Betclic"]["signals"] == 1
    assert reads == ["session_20261103_170000_aaaaaa"]  # read from the file, not the session

    cache_file.write_bytes(b"not a pickle")
    assert dashboard.load_flag_cache(cache_file) == {}


def test_a_recheck_between_runs_ends_or_extends_a_signal():
    sessions = ["session_20261103_170000_aaaaaa", "session_20261103_170500_bbbbbb", "session_20261103_171000_cccccc"]
    flagged = dashboard.table_records([at("17:00")])
    held = [{"Betclic": flagged}, {"Betclic": dashboard.table_records([at("17:05")])}, {}]

    def check(hhmm, ev, price=2.2):
        return {"book": "Betclic", "side": 2, "player1": "Ann Alpha", "player2": "Bea Beta",
                "tournament": "WTA Chennai - R16", "checked_at": f"2026-11-03T{hhmm}:00+00:00",
                "price": price, "ev": ev}

    # Gone at the recheck 2 minutes after the flag: ended there, not at 17:05.
    [life] = dashboard.signal_lifetimes(sessions, held, flagged, "Betclic", 0.02, {}, [check("17:02", -0.01)])
    assert life["ended"] and life["minutes"] == pytest.approx(2.0) and life["next_gone"]
    assert life["gap"] == pytest.approx(2.0)

    # Still bettable at the recheck: it lasts to the run that has it no more.
    [life] = dashboard.signal_lifetimes(sessions, held, flagged, "Betclic", 0.02, {}, [check("17:02", 0.05)])
    assert life["seen"] == pytest.approx(5.0) and not life["next_gone"]

    # Another book's recheck changes nothing.
    other = {**check("17:02", -0.01), "book": "Superbet"}
    [life] = dashboard.signal_lifetimes(sessions, held, flagged, "Betclic", 0.02, {}, [other])
    assert life["gap"] == pytest.approx(5.0)


def test_lasts_prefers_five_minute_runs_once_there_are_enough():
    def life(minutes, gap, seen=0.0):
        return {"minutes": minutes, "seen": seen, "ended": True, "next_gone": seen == 0, "gap": gap}

    slow = [life(15, 15)] * 20
    stats = dashboard.lifetime_stats(slow + [life(5, 5)] * 9)
    assert dashboard.lifetime_text(stats) == "< 15 min" and stats["lasts_spacing"] == 15

    fast = [life(5, 5)] * 7 + [life(10, 5, seen=5)] * 3
    stats = dashboard.lifetime_stats(slow + fast)
    assert dashboard.lifetime_text(stats) == "< 5 min" and stats["lasts_ended"] == 10
    assert stats["lasts_within"] == 0.7
    assert dashboard.lifetime_help(stats) == "70% of 10 signals were gone within 5 min, checked again 5 min after the odds were read."
    assert dashboard.lifetime_help({}) == "No signal has ended yet"

    # An even count: the upper middle signal, not a mean of two (Superbet, 15 Nov 2026).
    even = [life(15, 15)] * 5 + [life(30, 15, seen=15)] * 3 + [life(45, 15, seen=30), life(75, 15, seen=60)]
    stats = dashboard.lifetime_stats(even)
    assert dashboard.lifetime_text(stats) == "15–30 min" and stats["lasts_within"] == 0.8


def test_lasts_tile_shows_its_share_on_hover_or_tap():
    stats = dashboard.track_stats([
        {"Result": "Won", "_price": 2.0, "Fair prob": 0.5, "CLV": 0.05, "Best EV": 0.03},
    ])
    stats.update(dashboard.lifetime_stats([
        {"minutes": 14.9, "seen": 0.0, "ended": True, "next_gone": True, "gap": 15.0},
        {"minutes": 29.9, "seen": 15.0, "ended": True, "next_gone": False, "gap": 15.0},
        {"minutes": 14.8, "seen": 0.0, "ended": True, "next_gone": True, "gap": 15.0},
    ]))
    page = dashboard.track_card({"Betclic": stats}, 0.02)

    assert '<div class="overview-tile has-tip end" tabindex="0"><div class="value">&lt; 15 min</div>' in page
    assert '<div class="tip">67% of 3 signals were gone within 15 min, checked again 15 min after the odds were read.</div>' in page


def test_donut_keeps_one_match_visible():
    lengths = dashboard.donut_lengths([38, 1, 2], 100.0)
    assert sum(lengths) == pytest.approx(100.0)
    # 1 of 41 (2.4%) is lifted to 4%; 2 of 41 (4.9%) is not, and gives up its share.
    assert lengths[1] == pytest.approx(4.0)
    assert lengths[2] == pytest.approx(2 / 40 * 96)

    donut = dashboard.overview_donut("Betclic", 38, 1, "live 1", 41, 41)
    assert "conic-gradient(#2a78d6 0.00% " in donut and "transparent" not in donut
    assert 'title="EV calculated 38 of 41 (93%), No EV 1 of 41 (2%), Not matched 2 of 41 (5%)"' in donut
    assert '38<span class="donut-whole">/41</span>' in donut
    assert "(live 1)" in donut and "Waiting" not in donut

    whole = dashboard.overview_donut("Superbet", 5, 0, "", 5, 5)
    assert "conic-gradient(#2a78d6 0.00% 100.00%)" in whole and "transparent" not in whole

    empty = dashboard.overview_donut("Superbet", 0, 0, "", 0, 0)
    assert "conic-gradient" not in empty and "Waiting" not in empty
    assert "EV calculated <b>0</b>" in empty and "No EV" not in empty


def test_track_loader_reads_results_saved_before_the_long_shot_switch(tmp_path):
    results_file = tmp_path / "track_results.pkl"
    import pickle

    # The old layout: records alone, keys without long shots.
    results_file.write_bytes(pickle.dumps({(0.02, ("Betclic",)): {"Betclic": {"signals": 3}}}))

    loader = dashboard.TrackLoader({}, cache_file=None, results_file=results_file)
    assert loader.results == {(0.02, ("Betclic",), (0.0, 1.0)): {"Betclic": {"signals": 3}}}


def test_long_shots_can_be_left_out_of_the_first_flags():
    def flag(name, price, ev):
        return {"Bet on": name, "Opponent": "X", "Tournament": "T", "Best EV": ev,
                "_price": price, "_starts_at": "", "_captured_at": ""}

    longshot = flag("Outsider", 11.0, 0.10)   # fair (1 + 0.10) / 11 = 10%
    normal = flag("Favourite", 2.10, 0.05)    # fair 50%
    both = dashboard.first_flags([longshot, normal], "Betclic", 0.02, {})
    assert [f["Bet on"] for f in both] == ["Outsider", "Favourite"]
    without = dashboard.first_flags([longshot, normal], "Betclic", 0.02, {}, longshots=False)
    assert [f["Bet on"] for f in without] == ["Favourite"]


def test_sightings_come_back_from_their_file_after_a_restart(monkeypatch, tmp_path):
    import os
    import time

    import evaluate_ev
    import match_results

    raw = tmp_path / "raw"
    raw.mkdir()
    old = raw / "pinnacle_old.csv"
    old.write_text("x\n", encoding="utf-8")
    past = time.time() - 60
    os.utime(old, (past, past))
    monkeypatch.setattr(dashboard, "PINNACLE_RAW_DIR", raw)
    monkeypatch.setattr(match_results, "load_results", lambda: [])
    monkeypatch.setattr(match_results, "index_results", lambda results: {})
    monkeypatch.setattr(match_results, "read_covered_through", lambda: None)

    read = []

    def add_sightings(groups, paths):
        read.extend(path.name for path in paths)
        return set()

    monkeypatch.setattr(evaluate_ev, "add_sightings", add_sightings)

    cache_file = tmp_path / "sightings.pkl"
    held = {("A", "B", "T"): [{"captured_at": "2026-11-22T10:00:00Z"}]}
    dashboard.save_sightings_cache({"pinnacle_old.csv"}, held, cache_file)

    # A restarted server: the file's snapshot is not read again, only a new one.
    new = raw / "pinnacle_new.csv"
    new.write_text("x\n", encoding="utf-8")
    os.utime(new, (past, past))
    sources = dashboard.load_outcome_sources({"cache_file": cache_file})

    assert read == ["pinnacle_new.csv"]
    assert sources["sightings"] == held
    assert sources["sighting_files"] == {"pinnacle_old.csv", "pinnacle_new.csv"}
    # Saved again with the new file (the last save was "now", so only after a while).
    assert dashboard.load_sightings_cache(cache_file)["files"] == {"pinnacle_old.csv"}
    sources["sightings_saved"] = 0
    third = raw / "pinnacle_third.csv"
    third.write_text("x\n", encoding="utf-8")
    os.utime(third, (past, past))
    dashboard.load_outcome_sources(sources)
    assert len(dashboard.load_sightings_cache(cache_file)["files"]) == 3


def test_held_numbers_show_at_once_and_only_a_missing_record_spins():
    from types import SimpleNamespace

    loader = dashboard.TrackLoader({}, cache_file=None, results_file=None)
    key = (0.02, ("Betclic",), False)
    saved = {"Betclic": {"signals": 1}}

    # A newly opened tab with held numbers: no spinner and no tick.
    page = SimpleNamespace(session_state={})
    dashboard.longshots_setting(page, "box")
    assert dashboard.card_loading(page, "box", loader, key, saved) == (False, False)
    assert "box_fresh_tick" not in page.session_state

    # Nothing held yet: the title spins, then ticks once when it is in.
    assert dashboard.card_loading(page, "box", loader, key, None) == (True, False)
    assert dashboard.card_loading(page, "box", loader, key, saved) == (False, False)
    assert page.session_state.pop("box_fresh_tick")
    assert not dashboard.opening_loading(page, "box", loader, key, saved)
    assert "box_fresh_tick" not in page.session_state

    # A click spins the box only while the new choice has no record.
    page.session_state["box_pending"] = True
    assert not dashboard.toggle_loading(page, "box", loader, key, saved)
    assert dashboard.toggle_loading(page, "box", loader, key, None)


def test_the_refresher_waits_for_a_run_to_finish(monkeypatch):
    sessions = ["session_c", "session_b", "session_a"]
    running = {"session_c"}
    monkeypatch.setattr(dashboard, "list_sessions", lambda: sessions)
    monkeypatch.setattr(dashboard, "is_running", lambda session_id: session_id in running)

    # A run going on is ignored: the card counts the last finished one.
    assert dashboard.TrackLoader.finished_session() == "session_b"
    running.clear()
    assert dashboard.TrackLoader.finished_session() == "session_c"


def test_the_refresher_keeps_held_choices_current_in_the_background(monkeypatch, tmp_path):
    values = iter(range(1, 100))
    computed = []

    def record(sessions, cache, sources, min_ev, now, books, rechecks=None, longshots=True):
        computed.append((min_ev, longshots))
        return {"Betclic": {"signals": next(values)}}

    inputs = {"value": 1}
    monkeypatch.setattr(dashboard, "load_outcome_sources", lambda cache: {})
    monkeypatch.setattr(dashboard, "list_sessions", lambda: [])
    monkeypatch.setattr(dashboard, "track_record", record)
    results_file = tmp_path / "track_results.pkl"
    # Only the asked choices here; the precomputed ranges have their own test.
    monkeypatch.setattr(dashboard.TrackLoader, "precompute_keys", lambda self: [])
    loader = dashboard.TrackLoader({}, cache_file=None, results_file=results_file)
    monkeypatch.setattr(loader, "inputs_signature", lambda: inputs["value"])

    key = (0.02, ("Betclic",), (0.0, 1.0))
    assert loader.request_key(key, wait=2) == {"Betclic": {"signals": 1}}
    loader.thread.join(5)
    version = loader.version(key)

    # Asked again: the held record at once, nothing worked out.
    assert loader.request_key(key) == {"Betclic": {"signals": 1}}
    assert computed == [(0.02, (0.0, 1.0))]

    # Inputs changed (a new run): worked out again in the background, saved, and the
    # version moves so the page redraws; unchanged inputs do nothing.
    assert loader.refresh_once()
    loader.thread.join(5)
    assert loader.results[key] == {"Betclic": {"signals": 2}}
    assert loader.version(key) == version + 1
    assert not loader.refresh_once()
    assert len(computed) == 2

    # A restarted server shows the saved numbers and keeps the same choices current.
    second = dashboard.TrackLoader({}, cache_file=None, results_file=results_file)
    monkeypatch.setattr(second, "inputs_signature", lambda: inputs["value"])
    assert second.results[key] == {"Betclic": {"signals": 2}} and key in second.asked
    assert second.refresh_once()
    second.thread.join(5)
    assert second.results[key] == {"Betclic": {"signals": 3}}

    # A held choice the refresher did not keep is shown, then worked out again.
    other = (0.01, ("Betclic",), (0.0, 1.0))
    second.results[other] = {"Betclic": {"signals": 0}}
    assert second.request_key(other) == {"Betclic": {"signals": 0}}
    second.thread.join(5)
    assert second.results[other] == {"Betclic": {"signals": 4}}


def test_the_statistics_title_says_whether_its_numbers_are_up_to_date(monkeypatch):
    monkeypatch.setattr(dashboard, "load_outcome_sources", lambda cache: {})
    monkeypatch.setattr(dashboard, "list_sessions", lambda: [])
    monkeypatch.setattr(dashboard, "track_record", lambda *a, **k: {"Betclic": {"signals": 1}})
    monkeypatch.setattr(
        dashboard.TrackLoader, "finished_session",
        staticmethod(lambda: "session_20261126_172000_abcdef"),
    )
    loader = dashboard.TrackLoader({}, cache_file=None, results_file=None)
    inputs = {"value": 1}
    monkeypatch.setattr(loader, "inputs_signature", lambda: inputs["value"])
    key = (0.02, ("Betclic",), (0.0, 1.0))

    loader.refresh_once()
    loader.request_key(key, wait=2)
    if loader.thread:
        loader.thread.join(5)
    assert loader.status(key) == ("current", "session_20261126_172000_abcdef")
    pill = dashboard.stats_status_html(*loader.status(key))
    assert "Up to date" in pill and "session-time" not in pill

    # A run finished: Updating until its recount is in.
    inputs["value"] = 2
    loader.refresh_once()
    if loader.thread:
        loader.thread.join(5)
    assert loader.status(key)[0] == "current"
    loader.signed = 3
    assert loader.status(key)[0] == "updating"
    assert "Updating" in dashboard.stats_status_html("updating", None)


def test_probability_band_counts_and_filter():
    from market_compare import band_of, in_band

    assert band_of(True) == (0.0, 1.0) and band_of(False) == (0.15, 1.0)
    assert in_band(0.10, (0.0, 0.15)) and not in_band(0.15, (0.0, 0.15))
    assert in_band(1.0, (0.85, 1.0)) and not in_band(0.5, (0.85, 1.0))


def test_longshots_setting_reads_percent_as_a_band():
    from types import SimpleNamespace

    page = SimpleNamespace(session_state={})
    assert dashboard.longshots_setting(page, "band") == (0.0, 1.0)
    page.session_state["band"] = (15, 85)
    assert dashboard.longshots_setting(page, "band") == (0.15, 0.85)
    page.session_state["band"] = True  # an old checkbox value
    assert dashboard.longshots_setting(page, "band") == (0.0, 1.0)


def test_band_chart_counts_signals_per_stretch():
    from market_compare import band_counts

    flags = [{"Best EV": 0.0, "_price": price} for price in (10.0, 3.0, 1.5, 1.1, 1.05)]
    # 10%, 33%, 67%, 91%, 95%
    assert band_counts(flags) == [1, 1, 1, 2]
    record = {"Betclic": {"by_band": [0, 5, 2, 0]}, "Superbet": {"by_band": [2, 1, 0, 0]}}
    chart = dashboard.band_chart_html(record, {"Betclic": "Betclic", "Superbet": "Superbet"}, (0.15, 1.0))
    assert chart.count('class="band-bar"') == 8
    assert "Betclic · Underdogs (15–50%): 5 signals" in chart
    assert chart.count("band-column out") == 1  # long shots, outside the range
    assert dashboard.band_chart_html({"Old": {"signals": 3}}, {"Old": "Old"}, True) == ""


def test_every_probability_range_is_worked_out_after_a_run(monkeypatch):
    from types import SimpleNamespace

    computed = []
    monkeypatch.setattr(dashboard, "load_outcome_sources", lambda cache: {})
    monkeypatch.setattr(dashboard, "list_sessions", lambda: [])
    monkeypatch.setattr(dashboard, "track_record", lambda *args, longshots=True, **kw: (
        computed.append(longshots) or {"Betclic": {"signals": 1}}))
    loader = dashboard.TrackLoader({}, cache_file=None, results_file=None)
    monkeypatch.setattr(loader, "inputs_signature", lambda: 1)
    assert loader.refresh_once()
    loader.thread.join(10)
    assert len(dashboard.ALL_BANDS) == 10 and sorted(computed) == sorted(dashboard.ALL_BANDS)
    # The page asks with the same keys: the slider's percents as fractions.
    state = SimpleNamespace(session_state={"track_band": (15, 50)})
    band = dashboard.longshots_setting(state, "track_band")
    key = (dashboard.DEFAULT_MIN_EV_PERCENT / 100, tuple(dashboard.BOOKS), band)
    assert key in loader.results
    markets = dashboard.MarketTrackLoader({}, cache_file=None, results_file=None)
    assert (0.02, 0.04, (0.15, 0.5)) in markets.precompute_keys()


def test_band_chart_keeps_every_source_and_its_colour():
    from types import SimpleNamespace

    full = {"Betclic": {"by_band": [0, 26, 35, 7]}, "Superbet": {"by_band": [2, 16, 6, 0]}}
    only_superbet = {"Superbet": {"by_band": [2, 16, 6, 0], "signals": 2}}
    loader = SimpleNamespace(results={(0.02, ("Betclic", "Superbet"), (0.0, 1.0)): full})
    key = (0.02, ("Betclic", "Superbet"), (0.0, 0.15))
    record = dashboard.chart_record(loader, key, only_superbet)
    assert record is full  # long shots alone: Betclic's bars stay
    labels = {"Betclic": "Betclic", "Superbet": "Superbet"}
    # Without Betclic's data Superbet still gets its own (second) colour.
    text = dashboard.band_chart_html(only_superbet, labels, (0.0, 0.15))
    assert dashboard.BAND_CHART_COLOURS[1] in text and dashboard.BAND_CHART_COLOURS[0] not in text
    # Nothing held for the whole range: the chosen one's record.
    assert dashboard.chart_record(SimpleNamespace(results={}), key, only_superbet) is only_superbet
