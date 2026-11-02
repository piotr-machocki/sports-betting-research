"""Parsing of the PinnAPI response (src/betting/books/pinnacle.py). No network."""

from betting.books.pinnacle import CSV_FIELDS, parse_event

CAPTURED = "2026-11-02T13:00:00+00:00"


def event(**overrides) -> dict:
    data = {
        "event_id": 1639701789,
        "league_name": "ATP Paris - R1",
        "starts": "2026-11-02T18:00:00Z",
        "home": "Denis Shapovalov",
        "away": "Karen Khachanov",
        "event_type": "prematch",
        "parent_id": None,
        "is_have_odds": True,
        "periods": {
            "num_0": {"number": 0, "money_line": {"home": 2.45, "away": 1.6098}},
        },
    }
    data.update(overrides)
    return data


def test_match_keeps_event_id_and_scheduled_start():
    match, reason = parse_event(event(), CAPTURED)

    assert reason is None
    assert match["event_id"] == 1639701789
    assert match["starts_at"] == "2026-11-02T18:00:00Z"
    assert set(match) == set(CSV_FIELDS)


def test_response_without_id_or_start_still_gives_a_match():
    data = event()
    del data["event_id"], data["starts"]

    match, reason = parse_event(data, CAPTURED)

    assert reason is None
    assert match["event_id"] is None
    assert match["starts_at"] is None
