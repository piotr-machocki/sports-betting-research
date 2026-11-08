"""scripts/notify.py: picking bets, the message and the sent memory. No network."""

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import notify  # noqa: E402

NOW = datetime(2026, 11, 7, 18, 0, tzinfo=timezone.utc)


def row(**overrides) -> dict:
    data = {
        "observation_id": "1",
        "pinnacle_player1": "Ann Alpha",
        "pinnacle_player2": "Bea Beta",
        "pinnacle_tournament": "WTA Finals - R1",
        "pinnacle_starts_at": "2026-11-07T20:00:00Z",
        "betclic_captured_at": "2026-11-07T17:50:10+00:00",
        "betclic_odds_for_pin_p1": "1.8",
        "betclic_odds_for_pin_p2": "2.25",
        "probability1": "0.52",
        "probability2": "0.48",
        "ev1": "-0.064",
        "ev2": "0.08",
        "status": "FOUND",
    }
    data.update(overrides)
    return data


def test_picks_the_best_side_above_the_minimum():
    [bet] = notify.candidates([row()], 0.02, NOW)

    assert (bet["book"], bet["side"], bet["player"], bet["price"]) == ("Betclic", 2, "Bea Beta", 2.25)
    assert round(bet["fair"], 3) == round(1 / 0.48, 3)


def test_skips_low_ev_started_and_unconfirmed():
    assert notify.candidates([row(ev2="0.01")], 0.02, NOW) == []
    assert notify.candidates([row(pinnacle_starts_at="2026-11-07T17:00:00Z")], 0.02, NOW) == []
    assert notify.candidates([row(status="VERIFY")], 0.02, NOW) == []


def test_superbet_only_when_found():
    superbet = {
        "superbet_status": "FOUND", "superbet_ev1": "0.05", "superbet_ev2": "-0.2",
        "superbet_price1": "2.3", "superbet_odds1": "2.024", "ev2": "-0.1",
    }
    [bet] = notify.candidates([row(**superbet)], 0.02, NOW)
    assert (bet["book"], bet["side"]) == ("Superbet", 1)
    assert "2.30 (2.02 after tax)" in notify.message(bet)

    assert notify.candidates([row(**{**superbet, "superbet_status": "VERIFY"})], 0.02, NOW) == []


def test_message():
    [bet] = notify.candidates([row()], 0.02, NOW)
    text = notify.message(bet)

    assert text.startswith("🎾 EV +8.0% · Betclic 2.25 on Bea Beta")
    assert "Ann Alpha vs Bea Beta" in text
    assert "Pinnacle fair 2.08" in text


def test_each_bet_sent_once_and_failures_retried(tmp_path, monkeypatch):
    sent_path = tmp_path / "sent.json"
    messages = []
    monkeypatch.setattr(notify, "credentials", lambda: ("token", "chat"))
    monkeypatch.setattr(notify.live_monitor, "read_session_rows", lambda path: [row()])

    def fail(*_):
        raise notify.TelegramError("HTTP 500")

    monkeypatch.setattr(notify, "send", fail)
    result = notify.notify_session("s", now=NOW, sent_path=sent_path)
    assert (result["sent"], result["status"]) == (0, "failed: HTTP 500")

    monkeypatch.setattr(notify, "send", lambda token, chat, text: messages.append(text))
    assert notify.notify_session("s", now=NOW, sent_path=sent_path)["sent"] == 1
    assert notify.notify_session("s", now=NOW, sent_path=sent_path)["sent"] == 0
    assert len(messages) == 1


def test_without_credentials_does_nothing(monkeypatch):
    monkeypatch.setattr(notify, "credentials", lambda: None)
    assert notify.notify_session("s", now=NOW)["sent"] == 0
