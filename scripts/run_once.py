"""Collect Betclic + Pinnacle odds back-to-back, then run the EV comparison.

Betclic goes first because it is slow (browser scrolling) and fragile; Pinnacle
is a ~1s API call, so running it right after keeps both snapshots seconds apart.

Usage:  python scripts/run_once.py [--include-live] [--min-ev 0.02]
                                   [--max-age 15] [--top 20]
Arguments are passed straight through to compare_odds.
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import compare_odds  # noqa: E402
from betting.books import betclic, pinnacle  # noqa: E402  (importing loads .env)


def step(name: str, fn) -> None:
    print(f"\n=== {name} ===")
    t0 = time.perf_counter()
    fn()
    print(f"--- {name} done in {time.perf_counter() - t0:.1f}s ---")


def main() -> None:
    # Fail before the slow Betclic scrape if Pinnacle can't run anyway.
    if not os.environ.get("PINNAPI_KEY"):
        sys.exit("Set the PINNAPI_KEY environment variable.")

    step("Betclic", betclic.main)
    step("Pinnacle", pinnacle.main)
    step("Compare", compare_odds.main)  # parses sys.argv itself


if __name__ == "__main__":
    main()