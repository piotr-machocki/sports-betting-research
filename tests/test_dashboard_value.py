import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import dashboard  # noqa: E402
import dashboard_value as dv  # noqa: E402


def row(source, clv, margin, count=20, signals=30):
    return {
        "path": ("Tennis", "Moneyline", "Sharp vs soft"), "source": source, "loading": False,
        "signals": signals, "clv": clv, "clv_margin": margin, "clv_count": count,
        "clv_positive": 0.5, "won": 3, "lost": 2, "roi": 0.1, "units": 0.5, "ev": 0.05,
    }


def test_verdicts():
    assert dv.verdict(0.05, 0.02, 30) == "real"
    assert dv.verdict(0.05, 0.08, 30) == "promising"
    assert dv.verdict(-0.01, 0.05, 30) == "doubtful"
    assert dv.verdict(-0.2, 0.1, 30) == "none"
    assert dv.verdict(0.3, 0.01, dv.MIN_CLV_SIGNALS - 1) == "early"
    assert dv.verdict(None, None, 0) == "early"
    # Saved results from before clv_margin existed: judged on the mean alone.
    assert dv.verdict(0.02, None, 30) == "promising"


def test_rank_by_average_clv_then_no_clv_last():
    ranked = dv.rank([
        row("Kalshi", -0.25, 0.1),
        {"path": ("Tennis",), "source": "Loading", "loading": True},
        row("Betclic", 0.026, 0.03),
        row("Superbet", 0.049, 0.06),
        row("New", None, None, count=0),
        row("Lucky", 0.4, None, count=1),
    ])
    assert [r["source"] for r in ranked] == ["Superbet", "Betclic", "Kalshi", "Lucky", "New", "Loading"]
    assert ranked[2]["verdict"] == "none"
    assert ranked[5]["verdict"] == "loading"


def test_axis_limit_rounds_up_and_caps():
    assert dv.axis_limit([row("a", 0.03, 0.02)]) == 0.1
    assert dv.axis_limit([row("a", -0.25, 0.1)]) == 0.4
    assert dv.axis_limit([row("a", -0.9, 0.5)]) == 0.5
    assert dv.axis_limit([]) == 0.1


def test_page_html_lists_every_row_and_cuts_wide_ranges():
    text = dv.page_html([row("Betclic", 0.026, 0.03), row("Both", -0.284, 0.6)])
    assert text.index("Betclic") < text.index("Both")
    assert "cut-left" in text and "No value" not in text  # range crosses zero
    assert "Doubtful" in text and "Promising" in text


def test_track_stats_has_clv_margin():
    flags = [
        {"Result": "Won", "_price": 2.0, "Best EV": 0.05, "CLV": clv}
        for clv in (0.1, -0.1, 0.0)
    ]
    stats = dashboard.track_stats(flags)
    assert stats["clv_margin"] > 0
    assert dashboard.track_stats(flags[:1])["clv_margin"] is None
