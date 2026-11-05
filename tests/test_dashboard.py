"""The dashboard's data layer (scripts/dashboard.py). No Streamlit, no network."""

import sys
from datetime import datetime, timedelta, timezone
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
    assert names(ev_filter=dashboard.EV_CALCULATED) == [
        "Negative",
        "Zero",
        "Small",
        "Exact",
    ]
    assert names(ev_filter=dashboard.EV_POSITIVE) == ["Small", "Exact"]
    assert names(ev_filter=dashboard.EV_THRESHOLD, min_ev=0.02) == ["Exact"]
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
    assert "Superbet" not in text
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


def test_overview_rows_have_the_same_columns():
    with_superbet = [row(superbet_odds1="1.80", superbet_best_ev="0.01")]
    for rows, columns in ((with_superbet, 3), ([row()], 2)):
        summary = dashboard.session_summary(rows, {"pinnacle": {"matches": 2}}, 0.02, NOW)
        first, second = dashboard.overview_html(summary).split('<div class="overview-cards">')[1:]

        assert first.count("overview-card wide") == columns
        assert second.count("overview-card wide") == columns


def test_overview_of_a_run_without_rows():
    assert "Waiting 3" in overview_text([], {"pinnacle": {"matches": 3}})
    assert "EV calculated 0" in overview_text([])


def test_overview_marks_a_problem_with_a_sign():
    text = overview_text([row(**{**NO_EV, "status": "ERROR"})])

    assert "\u26a0 1 Errors" in text


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
