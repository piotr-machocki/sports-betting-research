from datetime import datetime, timezone

from betting.books import pinnacle


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 11, 15, hour, minute, 30, tzinfo=timezone.utc)


def test_numbered_keys_take_turns_by_five_minute_slot(monkeypatch):
    monkeypatch.setenv("PINNAPI_KEY_TENNIS", "single")
    for number in (1, 2, 3):
        monkeypatch.setenv(f"PINNAPI_KEY_TENNIS_{number}", f"key{number}")

    assert pinnacle.tennis_api_keys() == ["key1", "key2", "key3"]
    used = [pinnacle.tennis_api_key(at(hour, minute)) for hour in range(24) for minute in range(0, 60, 5)]
    assert used[:4] == ["key1", "key2", "key3", "key1"]
    assert [used.count(key) for key in ("key1", "key2", "key3")] == [96, 96, 96]
    assert pinnacle.tennis_api_key(at(10, 9)) == "key2"


def test_a_single_key_is_used_for_every_run(monkeypatch):
    for number in (1, 2, 3):
        monkeypatch.delenv(f"PINNAPI_KEY_TENNIS_{number}", raising=False)
    monkeypatch.delenv("PINNAPI_KEY_TENNIS", raising=False)
    monkeypatch.setenv("PINNAPI_KEY", "old")

    assert pinnacle.tennis_api_keys() == ["old"]
    assert pinnacle.tennis_api_key(at(13, 25)) == "old"

    monkeypatch.delenv("PINNAPI_KEY")
    assert pinnacle.tennis_api_key() is None
