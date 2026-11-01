"""Historical evaluation of the four devig methods on Pinnacle closing odds.

Reads the raw tennis-data.co.uk Excel files (ATP/WTA, data/historical/raw/),
reuses the loading, cleaning and devigging of scripts/load_historical.py and
betting.margin, and scores every method against the actual match outcomes.

Usage:
    python scripts/evaluate_devig.py
    python scripts/evaluate_devig.py --min-ev 0.02

Outputs (data/results/devig_evaluation/, separate from the live pipeline):
    historical/  cleaning, scores, paired differences, calibration, subgroups
    signals/     historical Pinnacle -> Bet365 EV signals per method
Charts are made afterwards by scripts/plot_devig.py.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import load_historical as lh  # noqa: E402
from betting.evaluation import brier, calibration_bins, log_loss, paired_difference  # noqa: E402
from betting.margin import METHODS  # noqa: E402
from betting.value import calculate_ev  # noqa: E402

OUT_DIR = ROOT / "data" / "results" / "devig_evaluation"
HIST_OUT = OUT_DIR / "historical"
SIGNALS_OUT = OUT_DIR / "signals"

BASELINE = "proportional"

# Pinnacle closing two-way overround outside this range is a data error
# (an underround cannot be devigged, and > 10% is far above Pinnacle's
# tennis margins: the 99.9th percentile of the raw data is ~6.8%).
MAX_OVERROUND = 1.10
# Same idea for Bet365 in the signal dataset (99.9th percentile ~9.3%).
MAX_SOFT_OVERROUND = 1.15

# Favourite probability bins for calibration: 50-60%, ..., 90%+.
CALIBRATION_EDGES = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
CALIBRATION_LABELS = ["50-60%", "60-70%", "70-80%", "80-90%", "90%+"]

# Ratio of underdog odds to favourite odds, a measure of market asymmetry.
ASYMMETRY_EDGES = [1.0, 1.5, 2.0, 3.0, 5.0, 10.0, np.inf]
ASYMMETRY_LABELS = ["1-1.5", "1.5-2", "2-3", "3-5", "5-10", "10+"]

VARIANTS = {
    # Main analysis: completed matches only (same rule as load_historical.py).
    "completed": {"Completed"},
    # Sensitivity: retirements kept, the player who advanced is the winner.
    "with_retirements": {"Completed", "Retired"},
}


def normalise_comments(raw: pd.DataFrame) -> pd.DataFrame:
    raw = raw.copy()
    raw["Comment"] = raw["Comment"].astype(str).str.strip()
    # One row in the raw data is spelt "Rrtired".
    raw["Comment"] = raw["Comment"].replace({"Rrtired": "Retired"})
    return raw


def prepare(raw: pd.DataFrame, valid_comments: set[str]) -> tuple[pd.DataFrame, list[dict]]:
    """Clean, filter implausible markets and devig. Returns matches and a cleaning log."""
    log = [{"step": "loaded rows", "rows": len(raw)}]

    matches = lh.clean_matches(raw, valid_comments=valid_comments)
    log.append({
        "step": f"comment in {sorted(valid_comments)}, valid Pinnacle odds, "
                "metadata, de-duplicated",
        "rows": len(matches),
    })

    overround = 1 / matches["PSW"] + 1 / matches["PSL"]
    matches = matches[(overround > 1) & (overround <= MAX_OVERROUND)].copy()
    log.append({
        "step": f"Pinnacle overround in (1, {MAX_OVERROUND}]",
        "rows": len(matches),
    })

    matches = lh.add_probabilities(matches).reset_index(drop=True)
    matches["overround"] = 1 / matches["PSW"] + 1 / matches["PSL"]
    return matches, log


def add_scores(matches: pd.DataFrame) -> pd.DataFrame:
    """Per-match scores plus the favourite view used for calibration."""
    m = matches.copy()
    ones = np.ones(len(m))

    # Winner-side probability scored against y = 1. For a two-way market this
    # is exactly the binary log loss / Brier of either side.
    for method in METHODS:
        m[f"ll_{method}"] = log_loss(m[f"p_{method}_w"], ones)
        m[f"bs_{method}"] = brier(m[f"p_{method}_w"], ones)

    # Every method is monotone in the raw implied probability, so all of them
    # agree on who the favourite is. Exact ties have no favourite.
    m["tie"] = m["PSW"] == m["PSL"]
    m["fav_won"] = (m["PSW"] < m["PSL"]).astype(int)
    for method in METHODS:
        m[f"p_{method}_fav"] = np.maximum(m[f"p_{method}_w"], m[f"p_{method}_l"])

    m["fav_odds"] = np.minimum(m["PSW"], m["PSL"])
    m["dog_odds"] = np.maximum(m["PSW"], m["PSL"])
    m["asymmetry"] = m["dog_odds"] / m["fav_odds"]
    m["asymmetry_bin"] = pd.cut(
        m["asymmetry"], ASYMMETRY_EDGES, labels=ASYMMETRY_LABELS, right=False
    )
    m["fav_bin"] = pd.cut(
        m[f"p_{BASELINE}_fav"], CALIBRATION_EDGES, labels=CALIBRATION_LABELS,
        right=False, include_lowest=True,
    )
    m["cluster"] = m["tour"] + "|" + m["year"].astype(str) + "|" + m["Tournament"].astype(str)
    return m


def score_table(m: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for method in METHODS:
        rows.append({
            "method": method,
            "n": len(m),
            "log_loss": m[f"ll_{method}"].mean(),
            "log_loss_se": m[f"ll_{method}"].std(ddof=1) / np.sqrt(len(m)),
            "brier": m[f"bs_{method}"].mean(),
            "brier_se": m[f"bs_{method}"].std(ddof=1) / np.sqrt(len(m)),
        })
    return pd.DataFrame(rows)


def paired_table(m: pd.DataFrame, metric: str, against: list[str] | None = None) -> pd.DataFrame:
    """Paired differences method - reference for `metric` ('ll' or 'bs')."""
    rows = []
    references = against or [BASELINE]
    for reference in references:
        for method in METHODS:
            if method == reference:
                continue
            stats = paired_difference(
                m[f"{metric}_{method}"], m[f"{metric}_{reference}"], clusters=m["cluster"]
            )
            rows.append({"method": method, "reference": reference, **stats})
    return pd.DataFrame(rows)


def subgroup_table(m: pd.DataFrame, by: str) -> pd.DataFrame:
    rows = []
    for key, group in m.groupby(by, observed=True):
        if len(group) < 2:
            continue
        for method in METHODS:
            if method == BASELINE:
                continue
            stats = paired_difference(group[f"ll_{method}"], group[f"ll_{BASELINE}"])
            rows.append({
                by: key,
                "method": method,
                "log_loss_method": group[f"ll_{method}"].mean(),
                "log_loss_proportional": group[f"ll_{BASELINE}"].mean(),
                **stats,
            })
    return pd.DataFrame(rows)


def calibration_table(m: pd.DataFrame) -> pd.DataFrame:
    """Favourite calibration, binned on proportional probability (common bins)
    and, as a sensitivity check, on each method's own probability."""
    fav = m[~m["tie"]]
    rows = []
    for binning in ("proportional", "own"):
        for method in METHODS:
            bin_by = fav[f"p_{BASELINE}_fav"] if binning == "proportional" else None
            for label, row in zip(
                CALIBRATION_LABELS,
                calibration_bins(fav[f"p_{method}_fav"], fav["fav_won"], CALIBRATION_EDGES, bin_by),
            ):
                rows.append({"binning": binning, "method": method, "bin": label, **row})
    return pd.DataFrame(rows)


def probability_difference_table(m: pd.DataFrame) -> pd.DataFrame:
    """Favourite probability minus proportional, by market asymmetry."""
    rows = []
    for key, group in m.groupby("asymmetry_bin", observed=True):
        for method in METHODS:
            if method == BASELINE:
                continue
            diff = group[f"p_{method}_fav"] - group[f"p_{BASELINE}_fav"]
            rows.append({
                "asymmetry_bin": key,
                "method": method,
                "n": len(group),
                "median_overround": group["overround"].median(),
                "mean_diff": diff.mean(),
                "mean_abs_diff": diff.abs().mean(),
                "p05_diff": diff.quantile(0.05),
                "p95_diff": diff.quantile(0.95),
                "max_abs_diff": diff.abs().max(),
            })
    return pd.DataFrame(rows)


# --- Historical EV signals (Pinnacle fair probability vs Bet365 price) ---

def build_sides(m: pd.DataFrame) -> pd.DataFrame:
    # Drop matches with an implausible Bet365 market (zero odds, underround,
    # obvious typos) before building the two sides per match.
    soft_overround = 1 / m["B365W"] + 1 / m["B365L"]
    m = m[m["B365W"].gt(1) & m["B365L"].gt(1)
          & (soft_overround > 1) & (soft_overround <= MAX_SOFT_OVERROUND)]
    sides = lh.to_sides(m)

    # to_sides stacks winner rows then loser rows, so match i is row i and i + n.
    n = len(sides) // 2
    opponent_odds = np.concatenate([sides["pinnacle_odds"].to_numpy()[n:],
                                    sides["pinnacle_odds"].to_numpy()[:n]])
    sides["is_favourite"] = sides["pinnacle_odds"].to_numpy() < opponent_odds
    sides["is_tie"] = sides["pinnacle_odds"].to_numpy() == opponent_odds

    for method in METHODS:
        sides[f"ev_{method}"] = [
            calculate_ev(p, o) for p, o in zip(sides[f"p_{method}"], sides["soft_odds"])
        ]
    return sides


def signal_tables(sides: pd.DataFrame, min_ev: float) -> dict[str, pd.DataFrame]:
    flags = {method: sides[f"ev_{method}"] >= min_ev for method in METHODS}
    profit = sides["y"] * sides["soft_odds"] - 1  # flat 1-unit stake

    counts = []
    for method, flag in flags.items():
        s = sides[flag]
        p = profit[flag]
        counts.append({
            "method": method,
            "min_ev": min_ev,
            "sides_evaluated": len(sides),
            "signals": int(flag.sum()),
            "favourite_signals": int((flag & sides["is_favourite"] & ~sides["is_tie"]).sum()),
            "underdog_signals": int((flag & ~sides["is_favourite"] & ~sides["is_tie"]).sum()),
            "tie_signals": int((flag & sides["is_tie"]).sum()),
            "unique_to_method": int((flag & ~pd.concat(
                [f for k, f in flags.items() if k != method], axis=1).any(axis=1)).sum()),
            "mean_ev_predicted": s[f"ev_{method}"].mean() if len(s) else np.nan,
            "realised_roi": p.mean() if len(p) else np.nan,
            "realised_roi_se": p.std(ddof=1) / np.sqrt(len(p)) if len(p) > 1 else np.nan,
        })

    overlap = []
    for a, fa in flags.items():
        for b, fb in flags.items():
            both = int((fa & fb).sum())
            union = int((fa | fb).sum())
            # Flat-stake result of the signals method A flags but B does not.
            a_only = profit[fa & ~fb]
            overlap.append({
                "method_a": a,
                "method_b": b,
                "both": both,
                "a_only": len(a_only),
                "jaccard": both / union if union else np.nan,
                "a_only_roi": a_only.mean() if len(a_only) else np.nan,
                "a_only_roi_se": a_only.std(ddof=1) / np.sqrt(len(a_only)) if len(a_only) > 1 else np.nan,
            })

    thresholds = []
    for threshold in (0.0, 0.01, 0.02, 0.03, 0.05, 0.10):
        for method in METHODS:
            thresholds.append({
                "min_ev": threshold,
                "method": method,
                "signals": int((sides[f"ev_{method}"] >= threshold).sum()),
            })

    return {
        "signal_counts": pd.DataFrame(counts),
        "signal_overlap": pd.DataFrame(overlap),
        "signal_counts_by_threshold": pd.DataFrame(thresholds),
    }


def save(df: pd.DataFrame, path: Path) -> None:
    df.to_csv(path, index=False, float_format="%.10g")
    print(f"[SAVED] {path.relative_to(ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--min-ev", type=float, default=0.02,
                        help="EV threshold for historical signals (live default: 0.02)")
    args = parser.parse_args()

    HIST_OUT.mkdir(parents=True, exist_ok=True)
    SIGNALS_OUT.mkdir(parents=True, exist_ok=True)

    raw = normalise_comments(lh.load_all())
    comment_counts = raw["Comment"].value_counts().rename_axis("comment").reset_index(name="rows")
    save(comment_counts, HIST_OUT / "raw_comment_counts.csv")

    summary = {"baseline": BASELINE, "min_ev": args.min_ev, "variants": {}}
    cleaning = []

    for variant, comments in VARIANTS.items():
        print(f"\n##### VARIANT: {variant} #####")
        matches, log = prepare(raw, comments)
        cleaning += [{"variant": variant, **row} for row in log]
        m = add_scores(matches)

        prefix = "" if variant == "completed" else f"{variant}_"
        scores = score_table(m)
        paired_ll = paired_table(m, "ll", against=list(METHODS))
        paired_bs = paired_table(m, "bs")
        save(scores, HIST_OUT / f"{prefix}scores.csv")
        save(paired_ll, HIST_OUT / f"{prefix}paired_log_loss.csv")
        save(paired_bs, HIST_OUT / f"{prefix}paired_brier.csv")

        summary["variants"][variant] = {
            "matches": len(m),
            "scores": scores.set_index("method").to_dict(orient="index"),
            "paired_log_loss_vs_proportional": paired_ll[paired_ll["reference"] == BASELINE]
            .set_index("method").drop(columns="reference").to_dict(orient="index"),
        }

        if variant != "completed":
            continue

        # Detailed outputs for the main analysis only.
        save(calibration_table(m), HIST_OUT / "calibration.csv")
        save(subgroup_table(m, "tour"), HIST_OUT / "paired_log_loss_by_tour.csv")
        save(subgroup_table(m, "year"), HIST_OUT / "paired_log_loss_by_year.csv")
        save(subgroup_table(m, "fav_bin"), HIST_OUT / "paired_log_loss_by_favourite_bin.csv")
        save(subgroup_table(m, "asymmetry_bin"), HIST_OUT / "paired_log_loss_by_asymmetry.csv")
        save(probability_difference_table(m), HIST_OUT / "probability_differences_by_asymmetry.csv")

        keep = ["Date", "Tournament", "Round", "tour", "year", "Winner", "Loser", "Comment",
                "PSW", "PSL", "B365W", "B365L", "overround", "asymmetry", "asymmetry_bin",
                "fav_bin", "tie", "fav_won"] \
            + [f"p_{k}_{s}" for k in METHODS for s in ("w", "l", "fav")] \
            + [f"{s}_{k}" for s in ("ll", "bs") for k in METHODS]
        m[keep].to_csv(HIST_OUT / "matches_evaluated.csv", index=False, date_format="%Y-%m-%d")
        print(f"[SAVED] {(HIST_OUT / 'matches_evaluated.csv').relative_to(ROOT)}")

        sides = build_sides(matches)
        tables = signal_tables(sides, args.min_ev)
        for name, table in tables.items():
            save(table, SIGNALS_OUT / f"{name}.csv")
        sides.to_csv(SIGNALS_OUT / "sides_evaluated.csv", index=False, date_format="%Y-%m-%d")
        print(f"[SAVED] {(SIGNALS_OUT / 'sides_evaluated.csv').relative_to(ROOT)}")
        summary["signals"] = {
            "sides_evaluated": len(sides),
            "counts": tables["signal_counts"].set_index("method").to_dict(orient="index"),
        }

    save(pd.DataFrame(cleaning), HIST_OUT / "cleaning_log.csv")
    with open(OUT_DIR / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=float)
    print(f"[SAVED] {(OUT_DIR / 'summary.json').relative_to(ROOT)}")


if __name__ == "__main__":
    main()
