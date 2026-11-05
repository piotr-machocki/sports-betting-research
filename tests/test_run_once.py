"""Which Betclic collector a run uses when it names none (scripts/run_once.py)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_once  # noqa: E402


@pytest.mark.parametrize(
    ("present", "expected"),
    [
        ({"feed", "scroll"}, "feed"),
        ({"scroll"}, "tournaments"),
        ({"feed"}, "search"),  # the feed reuses betclic_scroll.py
        (set(), "search"),
    ],
)
def test_default_mode_follows_the_files_present(tmp_path, monkeypatch, present, expected):
    for name, constant in (("feed", "BETCLIC_FEED_SCRIPT"), ("scroll", "BETCLIC_SCROLL_SCRIPT")):
        path = tmp_path / f"{name}.py"
        if name in present:
            path.touch()
        monkeypatch.setattr(run_once, constant, path)

    assert run_once.default_betclic_mode() == expected
