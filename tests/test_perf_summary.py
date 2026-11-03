"""Performance summary formatting (scripts/perf_summary.py)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import perf_summary  # noqa: E402


def betclic_lines(betclic: dict) -> list[str]:
    lines = perf_summary.performance_lines({"betclic": betclic})
    start = lines.index("Betclic")
    return lines[start:lines.index("", start)]


def test_phases_are_listed_under_collection_time():
    lines = betclic_lines({
        "mode": "hybrid",
        "seconds": 100.5,
        "phases": [
            {"name": "scroll", "seconds": 11.4, "matches": 68, "found": 25},
            {"name": "search", "seconds": 86.0, "matches": 43, "found": 40},
        ],
    })

    collection = lines.index("  Collection time:         1 min 40.50 s")
    assert lines[collection + 1:collection + 3] == [
        "    scroll:                11.40 s  (25 of 68 matches found)",
        "    search:                1 min 26.00 s  (40 of 43 matches found)",
    ]


def test_runs_without_phases_show_no_phase_lines():
    for betclic in ({"mode": "search", "seconds": 5.0}, {"phases": None}, {"phases": ["bad"]}):
        assert not any(line.startswith("    ") for line in betclic_lines(betclic))


def test_phase_without_a_time_shows_n_a():
    [line] = perf_summary.phase_lines([{"name": "search", "seconds": None, "matches": 0, "found": 0}])

    assert line == "    search:                n/a  (0 of 0 matches found)"
