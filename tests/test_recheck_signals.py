"""scripts/recheck_signals.py: picking a run's alerts and pricing them again. No network:
the collectors are replaced."""

import csv
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import recheck_signals  # noqa: E402

SESSION = "session_20261115_231502_abcdef"
SENT = {
    # This run's alert (24 s after its start) and an older one from another run.
    "Carlos Taberner|Filippo Moroni|ATP Challenger Metz - Qualifiers|Betclic|1": {
        "at": "2026-11-15T23:15:24+00:00", "price": 1.49, "ev": 0.1248,
    },
    "Ann Alpha|Bea Beta|WTA Finals - R1|Superbet|2": {
        "at": "2026-11-15T22:00:24+00:00", "price": 2.5, "ev": 0.05,
    },
}


def test_only_the_runs_own_alerts_are_rechecked():
    alerts = recheck_signals.run_alerts(SESSION, SENT)

    assert [(a["player1"], a["book"], a["side"]) for a in alerts] == [("Carlos Taberner", "Betclic", 1)]
    assert recheck_signals.run_alerts("session_20261115_232002_abcdef", SENT) == []


def test_price_now_says_gone_when_the_book_no_longer_offers_it_prematch():
    row = {
        "status": "FOUND", "betclic_status": "not_marked_live", "odds_ok": "True",
        "betclic_odds_for_pin_p1": "1.45", "pinnacle_odds1": "1.35", "pinnacle_odds2": "3.3",
    }
    price, ev = recheck_signals.price_now("Betclic", row, 1)
    assert price == 1.45 and ev == pytest.approx(1.45 * recheck_signals.fair_probability(row, 1) - 1)

    assert recheck_signals.price_now("Betclic", {**row, "betclic_status": "live"}, 1) == (None, None)
    assert recheck_signals.price_now("Betclic", None, 1) == (None, None)

    superbet = {"superbet_status": "FOUND", "superbet_price2": "2.4", "superbet_ev2": "0.01"}
    assert recheck_signals.price_now("Superbet", superbet, 2) == (2.4, 0.01)
    assert recheck_signals.price_now("Superbet", {"superbet_status": "VERIFY"}, 2) == (None, None)


def test_recheck_prices_the_alert_against_the_same_pinnacle_snapshot(tmp_path, monkeypatch):
    raw = tmp_path / "pinnacle"
    raw.mkdir()
    with (raw / f"pinnacle_{SESSION}.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["player1", "odds1", "player2", "odds2", "tournament"])
        writer.writeheader()
        writer.writerow({"player1": "Carlos Taberner", "odds1": "1.6", "player2": "Filippo Moroni",
                         "odds2": "2.4", "tournament": "ATP Challenger Metz - Qualifiers"})
        writer.writerow({"player1": "Other One", "odds1": "1.5", "player2": "Other Two",
                         "odds2": "2.6", "tournament": "Elsewhere"})

    monkeypatch.setattr(recheck_signals, "PINNACLE_RAW_DIR", raw)
    monkeypatch.setattr(recheck_signals, "RECHECK_DIR", tmp_path / "rechecks")
    monkeypatch.setattr(recheck_signals, "RECHECKS_FILE", tmp_path / "rechecks" / "rechecks.jsonl")
    monkeypatch.setattr(recheck_signals.notify, "load_sent", lambda: SENT)
    seen = {}

    def collect(book, recheck_id, pinnacle_csv, out_dir):
        seen["matches"] = [row["player1"] for row in csv.DictReader(pinnacle_csv.open(encoding="utf-8"))]
        seen["book"], seen["id"] = book, recheck_id
        return [{
            "pinnacle_player1": "Carlos Taberner", "pinnacle_player2": "Filippo Moroni",
            "pinnacle_tournament": "ATP Challenger Metz - Qualifiers", "pinnacle_odds1": "1.6",
            "pinnacle_odds2": "2.4", "status": "FOUND", "betclic_status": "not_marked_live",
            "odds_ok": "True", "betclic_odds_for_pin_p1": "1.45",
        }]

    monkeypatch.setattr(recheck_signals, "collect", collect)
    [record] = recheck_signals.recheck(SESSION, 150, wait=False)

    # Only the alerted match went to the collector, under a recheck id.
    assert seen == {"matches": ["Carlos Taberner"], "book": "Betclic", "id": "recheck_20261115_231502_abcdef"}
    assert record["price"] == 1.45 and record["alert_price"] == 1.49
    assert record["ev"] == pytest.approx(1.45 * recheck_signals.remove_margin([1.6, 2.4], "power")[0] - 1)
    assert json.loads((tmp_path / "rechecks" / "rechecks.jsonl").read_text())["side"] == 1
    assert recheck_signals.load_rechecks(tmp_path / "rechecks" / "rechecks.jsonl")[0]["book"] == "Betclic"
    assert "Betclic Carlos Taberner 1.49 (+12.5%) -> 1.45" in recheck_signals.describe(record)


def test_no_alert_means_no_request(monkeypatch):
    monkeypatch.setattr(recheck_signals.notify, "load_sent", lambda: {})
    monkeypatch.setattr(recheck_signals, "collect", lambda *a: pytest.fail("no request without an alert"))

    assert recheck_signals.recheck(SESSION, 150, wait=False) == []
