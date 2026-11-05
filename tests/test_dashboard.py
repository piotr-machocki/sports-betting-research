"""The dashboard's data layer (scripts/dashboard.py). No Streamlit, no network."""

import sys
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


def test_session_bar_shows_the_status_and_run_time():
    bar = session_bar()

    assert ">Finished</span>" in bar
    assert ">Run on</span>" in bar
    assert dashboard.session_label("session_20261105_151211_abcdef") in bar


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

    assert "Overview" in text
    assert "EV calculated 1" in text
    assert "No EV 1" in text
    assert "Found, no EV" not in text
    assert "Not matched 2" in text
    assert "Waiting 2" in text
    assert "Betclic 2 / 6" in text


def test_overview_hides_waiting_and_superbet_when_they_do_not_apply():
    text = overview_text([row()], {"pinnacle": {"matches": 1}})

    assert "Waiting" not in text
    assert "Superbet" not in text.replace("-- Superbet gap", "")
    assert "Processed 1 / 1" in text


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
    superbet = text[text.index("Superbet EV status") :]

    assert "EV calculated 1 No EV 1 Not matched 2 Waiting 1" in superbet
    assert "Betclic EV status EV calculated 4" in text


def test_overview_puts_the_status_cards_in_the_first_row():
    rows = [row(superbet_odds1="1.80", superbet_best_ev="0.01")]
    summary = dashboard.session_summary(rows, {"pinnacle": {"matches": 2}}, 0.02, NOW)
    first, second = dashboard.overview_html(summary).split('<div class="overview-cards">')[1:]

    assert "Betclic EV status" in first and "Superbet EV status" in first
    assert "Positive EV" in second and "Checks" in second


def test_overview_with_the_ev_filter_keeps_only_the_positive_ev_card():
    rows = [row(superbet_odds1="1.80", superbet_best_ev="0.01")]
    summary = dashboard.session_summary(rows, {"pinnacle": {"matches": 2}}, 0.02, NOW)
    page = dashboard.overview_html(summary, ev_only=True)

    assert page.count('<div class="overview-cards">') == 1
    assert "Positive EV" in page
    assert "Checks" not in page and "EV status" not in page and "spacer" not in page
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
    for rows, columns in ((with_superbet, 3), ([row()], 2)):
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

    assert "13.0s Betclic gap" in text
    assert "2.0s Superbet gap" in text


def test_checks_card_shows_the_superbet_gap_right_after_betclic_even_without_data():
    assert "10.0s Betclic gap -- Superbet gap" in overview_text([row()])


def test_checks_card_shows_the_gap_in_minutes_past_a_minute():
    rows = [row(betclic_captured_at="2026-11-03T17:51:05+00:00")]

    assert "1m05s Betclic gap" in overview_text(rows)


def test_checks_card_starts_with_the_pinnacle_data_age():
    checks = overview_text([row()]).split("Checks ")[1]

    assert checks.startswith("10 min 00 s Pinnacle data age")


def test_checks_card_drops_the_seconds_once_the_data_is_stale():
    assert dashboard.pinnacle_age_text(14 * 60 + 5) == "14 min 05 s"
    assert dashboard.pinnacle_age_text(15 * 60) == "15 min 00 s"
    assert dashboard.pinnacle_age_text(15 * 60 + 1) == "15 min"
    assert dashboard.pinnacle_age_text(59 * 60 + 59) == "59 min"
    assert dashboard.pinnacle_age_text(3600 + 2 * 60) == "1 h 02 min"


def test_checks_card_gives_the_browser_the_capture_time_to_count_from():
    summary = dashboard.session_summary([row()], {}, 0.02, NOW)
    captured = (NOW - timedelta(minutes=10)).timestamp()

    assert summary["pinnacle_captured"] == captured
    assert f'data-since="{captured}" data-stale-after="900"' in dashboard.overview_html(summary)


def test_checks_card_says_whether_the_data_is_stale():
    at_limit = "2026-11-03T17:45:00+00:00"
    past_limit = "2026-11-03T17:44:59+00:00"

    assert "No Stale (over 15 min)" in overview_text([row(pinnacle_captured_at=at_limit)])
    assert "Yes Stale (over 15 min)" in overview_text(
        [row(pinnacle_captured_at=past_limit)]
    )


def test_checks_card_without_prices():
    text = overview_text([])

    assert "-- Pinnacle data age" in text
    assert "-- Betclic gap" in text
    assert "-- Stale (over 15 min)" in text


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
    assert "Betclic EV status" in page and "Betclic gap" in page
    assert "spacer" not in page


def test_overview_with_betclic_off_is_superbet_only():
    page = overview_for([row(**BOTH_BOOKS, status="ERROR")], ["Superbet"])

    assert "Betclic" not in page
    assert "Superbet EV status" in page and "Superbet gap" in page
    assert "Superbet &gt; 0%" in page or "Superbet > 0%" in page
    assert ">Errors<" not in page
    first, second = page.split('<div class="overview-cards">')[1:]
    assert "Pinnacle matches covered" in first and "Superbet EV status" in first
    assert "spacer" not in page


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
    assert any("Tennis EV" in block.value for block in app.markdown)
