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


# ------------------------------------------------------------ failed runs ---

def at(hour: int, day: int = 9) -> datetime:
    return datetime(2026, 11, day, hour, 0, tzinfo=timezone.utc)


def test_alert_after_three_failed_runs_in_a_row_at_most_once_a_day(tmp_path):
    path = tmp_path / "failures.json"
    sent = []

    def run(failure, now):
        return notify.notify_run(failure, now, path, sent.append)

    run("ERROR: boom", at(10))
    run("ERROR: boom", at(11))
    assert sent == []

    assert run("ERROR: boom", at(12))["alerted"]
    assert "3 runs failed in a row" in sent[0] and "ERROR: boom" in sent[0]

    run("ERROR: boom", at(13))
    run(None, at(14))
    for hour in (15, 16, 17):
        run("ERROR: again", at(hour))
    assert len(sent) == 1  # already alerted today

    assert run("ERROR: again", at(10, day=10))["alerted"]  # still failing the next day
    assert len(sent) == 2 and "4 runs failed in a row since Mon 09 Nov" in sent[1]


def test_a_finished_run_resets_the_streak(tmp_path):
    path = tmp_path / "failures.json"
    sent = []

    for failure in ("x", "x", None, "x", "x"):
        result = notify.notify_run(failure, at(10), path, sent.append)

    assert result["streak"] == 2 and sent == []


def test_a_failed_alert_is_tried_again_on_the_next_failed_run(tmp_path):
    path = tmp_path / "failures.json"

    def broken(text):
        raise notify.TelegramError("HTTP 502")

    for hour in (10, 11):
        notify.notify_run("x", at(hour), path, broken)
    assert notify.notify_run("x", at(12), path, broken)["status"] == "alert failed: HTTP 502"

    sent = []
    assert notify.notify_run("x", at(13), path, sent.append)["alerted"]


def test_run_failure_reads_the_session_status_or_the_log(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    log = tmp_path / "run.log"
    log.write_text(
        "===== 2026-11-09 10:00:00 UTC =====\nERROR: old\nexit code 1\n"
        "===== 2026-11-09 10:15:00 UTC =====\n"
        "===== 2026-11-09 10:30:00 UTC: previous run still going, skipped =====\n"
        "ERROR: Pinnacle collection failed: HTTPError: 429\nexit code 1\n",
        encoding="utf-8",
    )
    started = datetime(2000, 1, 1, tzinfo=timezone.utc)

    assert notify.run_failure(0, started, sessions, log) is None
    assert notify.run_failure(1, started, sessions, log) == (
        "ERROR: Pinnacle collection failed: HTTPError: 429"
    )

    status = sessions / "session_20261109_101500_abcdef_status.json"
    status.write_text('{"status": "failed", "error": "Betclic process exited with code 1."}')
    assert notify.run_failure(0, started, sessions, log) == "Betclic process exited with code 1."

    status.write_text('{"status": "finished"}')
    assert notify.run_failure(0, started, sessions, log) is None

    later = datetime(2100, 1, 1, tzinfo=timezone.utc)  # session older than the run
    status.write_text('{"status": "failed"}')
    assert notify.run_failure(0, later, sessions, log) is None
