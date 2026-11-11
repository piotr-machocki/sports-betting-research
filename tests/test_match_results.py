"""Results fetcher and Pinnacle linking (scripts/match_results.py). No network."""

import json
import sys

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import match_results as mr  # noqa: E402


def provider_row(**overrides) -> dict:
    row = {
        "id": "1",
        "date": "2026-11-01T00:00:00.000Z",
        "roundId": 1,
        "player1Id": 10,
        "player2Id": 20,
        "match_winner": 10,
        "result": "6-4 6-4",
        "result_type": "completed",
        "odd1": "1.31",
        "odd2": "3.08",
        "player1": {"id": 10, "name": "Aidan Mayo"},
        "player2": {"id": 20, "name": "Duncan Chan"},
        "tournament": {"name": "Charlottesville Challenger"},
    }
    row.update(overrides)
    return row


def result(winner="Aidan Mayo", loser="Duncan Chan", day="2026-11-01", **extra) -> dict:
    return {"winner": winner, "loser": loser, "date": day, "result_type": "completed", **extra}


def link(player1, player2, results, starts=None, captured="2026-11-01T10:00:00+00:00", tournament=None):
    return mr.link_match(player1, player2, mr.index_results(results), starts, captured, tournament)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mr, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(mr, "RESULTS_CSV", tmp_path / "results.csv")
    monkeypatch.setattr(mr, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(mr, "first_snapshot_date", lambda: None)
    monkeypatch.setenv("RAPIDAPI_KEY", "test-key")
    return tmp_path


class FakeResponse:
    def __init__(self, body, status=200, remaining="40"):
        self.body = body
        self.status_code = status
        self.headers = {"X-RateLimit-Requests-Remaining": remaining}

    def json(self):
        return self.body

    def raise_for_status(self):
        pass


def fake_get(responses):
    calls = []

    def get(url, params, headers, timeout):
        calls.append((url, params["pageNo"]))
        return responses.pop(0) if isinstance(responses, list) else responses

    return get, calls


# ---------- parsing ----------

def test_parse_row_uses_match_winner_not_player_order():
    swapped = mr.parse_row(provider_row(match_winner=20), "atp")

    assert swapped["winner"] == "Duncan Chan"
    assert swapped["loser"] == "Aidan Mayo"
    assert swapped["odds_winner"] == "3.08"
    assert set(swapped) == set(mr.RESULT_FIELDS)


def test_parse_row_without_winner_is_dropped():
    assert mr.parse_row(provider_row(match_winner=None), "atp") is None


def test_parse_row_flags_doubles():
    row = provider_row(player1={"id": 10, "name": "Sara Errani/Jasmine Paolini"})
    assert mr.parse_row(row, "wta")["is_doubles"] is True


def test_store_merges_by_id_and_later_fetch_wins(data_dir):
    mr.store_rows([provider_row(id="1", result="old"), provider_row(id="2")], "atp")
    mr.store_rows([provider_row(id="1", result="new")], "atp")

    saved = json.loads((data_dir / "raw" / "2026-11-01_atp.json").read_text(encoding="utf-8"))

    assert {r["id"]: r["result"] for r in saved} == {"1": "new", "2": "6-4 6-4"}
    assert mr.rebuild_csv() == 2


# ---------- once a day ----------

def test_no_request_while_yesterday_is_covered(data_dir, monkeypatch):
    write_state_at(date(2026, 11, 1), "2026-11-02T00:00:24+00:00")
    monkeypatch.setattr(mr.requests, "get", lambda *a, **k: pytest.fail("made a request"))

    summary = mr.update(now=datetime(2026, 11, 2, 7, 0, tzinfo=timezone.utc))

    assert summary["status"] == "up to date"
    assert summary["requests"] == 0


def write_state_at(covered: date, fetched_at: str) -> None:
    mr.STATE_FILE.write_text(
        json.dumps({"covered_through": covered.isoformat(), "last_fetch_at": fetched_at}),
        encoding="utf-8",
    )


def test_rechecks_once_after_the_recheck_hour(data_dir, monkeypatch):
    # Covered by the fetch just after midnight; late matches of yesterday may be missing.
    write_state_at(date(2026, 11, 1), "2026-11-02T00:00:24+00:00")
    get, calls = fake_get(FakeResponse({"data": [provider_row()], "hasNextPage": False}))
    monkeypatch.setattr(mr.requests, "get", get)

    before = mr.update(now=datetime(2026, 11, 2, 7, 45, tzinfo=timezone.utc))
    assert before["requests"] == 0

    after = mr.update(now=datetime(2026, 11, 2, 8, 0, 5, tzinfo=timezone.utc))
    assert after["requests"] == 2
    assert "2026-10-30/2026-11-01" in calls[0][0]
    assert mr.read_covered_through() == date(2026, 11, 1)

    # Rewritten past the hour: no third fetch that day.
    write_state_at(date(2026, 11, 1), "2026-11-02T08:00:09+00:00")
    monkeypatch.setattr(mr.requests, "get", lambda *a, **k: pytest.fail("made a request"))
    late = mr.update(now=datetime(2026, 11, 2, 23, 0, tzinfo=timezone.utc))
    assert late["status"] == "up to date"


def test_recheck_due():
    utc = timezone.utc
    now = datetime(2026, 11, 2, 9, 0, tzinfo=utc)
    assert mr.recheck_due(None, now)
    assert mr.recheck_due(datetime(2026, 11, 2, 0, 0, tzinfo=utc), now)
    assert not mr.recheck_due(datetime(2026, 11, 2, 8, 30, tzinfo=utc), now)
    assert not mr.recheck_due(datetime(2026, 11, 2, 0, 0, tzinfo=utc), now.replace(hour=7))


def test_catches_up_after_days_off_and_rechecks_recent_days(data_dir, monkeypatch):
    mr.write_state(date(2026, 10, 20))
    get, calls = fake_get(FakeResponse({"data": [provider_row()], "hasNextPage": False}))
    monkeypatch.setattr(mr.requests, "get", get)

    summary = mr.update(today=date(2026, 11, 2))

    assert "2026-10-21/2026-10-27" in calls[0][0]
    assert "2026-10-28/2026-11-01" in calls[2][0]
    assert summary["covered_through"] == date(2026, 11, 1)
    assert mr.read_covered_through() == date(2026, 11, 1)


def test_start_rechecks_days_already_covered():
    assert mr.start_date(date(2026, 11, 1), date(2026, 11, 2), None) == date(2026, 10, 31)


def test_first_run_starts_at_oldest_snapshot():
    assert mr.start_date(None, date(2026, 11, 1), date(2026, 10, 11)) == date(2026, 10, 11)


def test_follows_pages(data_dir, monkeypatch):
    get, calls = fake_get(
        [
            FakeResponse({"data": [provider_row(id="1")], "hasNextPage": True}),
            FakeResponse({"data": [provider_row(id="2")], "hasNextPage": False}),
            FakeResponse({"data": [provider_row(id="3")], "hasNextPage": False}),
        ]
    )
    monkeypatch.setattr(mr.requests, "get", get)

    summary = mr.update(today=date(2026, 11, 2), start=date(2026, 11, 1))

    assert [page for _, page in calls] == [1, 2, 1]
    assert summary["results"] == 3


def test_rate_limit_is_not_retried_and_does_not_advance(data_dir, monkeypatch):
    mr.write_state(date(2026, 10, 31))
    get, calls = fake_get(FakeResponse({}, status=429))
    monkeypatch.setattr(mr.requests, "get", get)

    summary = mr.update(today=date(2026, 11, 2))

    assert len(calls) == 1
    assert summary["status"].startswith("stopped early")
    assert mr.read_covered_through() == date(2026, 10, 31)


def test_empty_response_does_not_advance(data_dir, monkeypatch):
    mr.write_state(date(2026, 10, 31))
    get, _ = fake_get(FakeResponse({"data": [], "hasNextPage": False}))
    monkeypatch.setattr(mr.requests, "get", get)

    mr.update(today=date(2026, 11, 2))

    assert mr.read_covered_through() == date(2026, 10, 31)


def test_half_fetched_chunk_does_not_advance(data_dir, monkeypatch):
    mr.write_state(date(2026, 10, 31))
    get, _ = fake_get(
        [
            FakeResponse({"data": [provider_row()], "hasNextPage": False}),
            FakeResponse({}, status=429),
        ]
    )
    monkeypatch.setattr(mr.requests, "get", get)

    mr.update(today=date(2026, 11, 2))

    assert mr.read_covered_through() == date(2026, 10, 31)


def test_stops_when_daily_quota_runs_low(data_dir, monkeypatch):
    get, calls = fake_get(
        FakeResponse({"data": [provider_row()], "hasNextPage": False}, remaining=str(mr.RESERVE_REQUESTS))
    )
    monkeypatch.setattr(mr.requests, "get", get)

    summary = mr.update(today=date(2026, 11, 2), start=date(2026, 11, 1))

    assert len(calls) == 1
    assert summary["status"].startswith("stopped early")


def test_missing_key_makes_no_request(data_dir, monkeypatch):
    monkeypatch.delenv("RAPIDAPI_KEY")
    monkeypatch.setattr(mr.requests, "get", lambda *a, **k: pytest.fail("made a request"))

    assert "RAPIDAPI_KEY" in mr.update(today=date(2026, 11, 2))["status"]


def test_run_step_never_raises(capsys, monkeypatch):
    def boom():
        raise OSError("disk full")

    monkeypatch.setattr(mr, "update", boom)
    mr.run_step()

    assert "WARNING" in capsys.readouterr().out


# ---------- linking ----------

@pytest.mark.parametrize(
    ("player1", "player2", "side"),
    [("Aidan Mayo", "Duncan Chan", 1), ("Duncan Chan", "Aidan Mayo", 2)],
)
def test_winner_side_follows_pinnacle_player_order(player1, player2, side):
    outcome = link(player1, player2, [result()])

    assert outcome["winner_side"] == side
    assert outcome["confidence"] == "exact"


def test_middle_name_and_accents_and_name_order_link():
    outcome = link(
        "Bianca Andreescu", "Jiale Zhang", [result("Bianca Vanessa Andreescu", "Zhang Jiale")]
    )

    assert outcome["winner_side"] == 1
    assert outcome["confidence"] == "partial"
    assert link("Iga Świątek", "Magda Linette", [result("Iga Swiatek", "Magda Linette")])["linked"]


def test_small_spelling_difference_is_fuzzy():
    outcome = link("Daniil Medvedev", "Duncan Chan", [result("Daniil Medvedew", "Duncan Chan")])

    assert outcome["confidence"] == "fuzzy"


@pytest.mark.parametrize(
    ("pinnacle", "provider"),
    [
        ("Yi-Fan Xu", "Yifan Xu"),
        ("Yvonne Cavalle-Reimers", "Ivonne Cavalle-Reimers"),
        ("Digvijay Pratap Singh", "Digvijaypratap Singh"),
        ("Victoria Veleva", "Viktoria Veleva"),
    ],
)
def test_spelling_variants_link(pinnacle, provider):
    outcome = link(pinnacle, "Duncan Chan", [result(provider, "Duncan Chan")])

    assert outcome["confidence"] == "fuzzy"


def test_shared_surname_is_not_the_same_player():
    assert link("Ivan Ivanov", "Duncan Chan", [result("Ilia Ivanov", "Duncan Chan")])["linked"] is False


def test_only_one_player_matching_does_not_link():
    assert link("Aidan Mayo", "Someone Else", [result()])["linked"] is False


def test_doubles_link_in_any_partner_order_and_never_to_singles():
    doubles = result("Sara Errani/Jasmine Paolini", "Kirsten Flipkens/Elise Mertens")

    assert link("Jasmine Paolini / Sara Errani", "Elise Mertens / Kirsten Flipkens", [doubles])["winner_side"] == 1
    assert link("Sara Errani", "Elise Mertens", [doubles])["linked"] is False


def test_outside_date_window_does_not_link():
    late = result(day="2026-11-05")

    assert link("Aidan Mayo", "Duncan Chan", [late], starts="2026-11-01T18:00:00Z")["linked"] is False


def test_rematch_picks_closest_day():
    results = [result(day="2026-10-30"), result("Duncan Chan", "Aidan Mayo", day="2026-11-01")]

    assert link("Aidan Mayo", "Duncan Chan", results, starts="2026-11-01T18:00:00Z")["winner_side"] == 2


def test_rematch_picks_same_tournament_over_closer_day():
    results = [
        result(day="2026-11-01", tournament="Queretaro Open - Queretaro"),
        result("Duncan Chan", "Aidan Mayo", day="2026-11-03", tournament="Cali Open - Cali"),
    ]

    outcome = link("Aidan Mayo", "Duncan Chan", results, tournament="WTA 125k Cali - Final")

    assert outcome["winner_side"] == 2


def test_misspelt_tournament_still_links():
    results = [result(tournament="Ningboo Challenger")]

    assert link("Aidan Mayo", "Duncan Chan", results, tournament="ATP Challenger Ningbo - R1")["linked"]


@pytest.mark.parametrize(
    ("name", "words"),
    [
        ("ATP Challenger Bratislava - Qualifiers", {"bratislava"}),
        ("WTA 125k Cali - Doubles", {"cali"}),
        ("ITF Women New Braunfels - Final", {"new", "braunfels"}),
        ("W35 Hamamatsu", {"hamamatsu"}),
        ("ATP Challenger Seoul - R16", {"seoul"}),
    ],
)
def test_tournament_words_keep_only_the_place(name, words):
    assert mr.tournament_words(name) == words


def test_provider_city_after_the_dash_counts_as_the_place():
    assert mr.tournament_words("WTA Tokyo - R1") & mr.tournament_words("Toray Pan Pacific Open - Tokyo")


def test_equally_good_results_that_disagree_are_ambiguous():
    results = [result(), result("Duncan Chan", "Aidan Mayo")]

    outcome = link("Aidan Mayo", "Duncan Chan", results)

    assert outcome == {"linked": False, "reason": "ambiguous"}


def test_linked_result_has_no_internal_fields():
    outcome = link("Aidan Mayo", "Duncan Chan", [result()])

    assert not [key for key in outcome["result"] if key.startswith("_")]
