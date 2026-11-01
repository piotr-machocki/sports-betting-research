"""Charts for the devig evaluation. Run scripts/evaluate_devig.py first.

Usage:
    python scripts/plot_devig.py

Reads data/results/devig_evaluation/ and writes PNGs (300 dpi) to
data/results/devig_evaluation/figures/. A copy goes to
docs/figures/devig_evaluation/ so docs/methodology.md can show them
(data/ is not committed).
"""
import shutil
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "data" / "results" / "devig_evaluation"
HIST = RESULTS / "historical"
SIGNALS = RESULTS / "signals"
FIG_DIR = RESULTS / "figures"
DOCS_FIG_DIR = ROOT / "docs" / "figures" / "devig_evaluation"

METHODS = ["proportional", "power", "shin", "odds_ratio"]
ALTERNATIVES = METHODS[1:]
LABELS = {"proportional": "Proportional", "power": "Power", "shin": "Shin", "odds_ratio": "Odds Ratio"}
# Fixed colour per method (validated categorical slots: blue, orange, aqua, violet).
COLORS = {"proportional": "#2a78d6", "power": "#eb6834", "shin": "#1baf7a", "odds_ratio": "#6250d6"}
MARKERS = {"proportional": "o", "power": "s", "shin": "^", "odds_ratio": "D"}

SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
GRID = "#e4e3df"
BLUES = ["#f0efec", "#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]

plt.rcParams.update({
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "font.size": 10,
    "axes.titlesize": 11,
    "axes.titleweight": "bold",
    "axes.labelcolor": TEXT_2,
    "axes.edgecolor": GRID,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.color": GRID,
    "grid.linewidth": 0.6,
    "axes.axisbelow": True,
    "xtick.color": TEXT_2,
    "ytick.color": TEXT_2,
    "text.color": TEXT,
    "legend.frameon": False,
})


def save(fig: plt.Figure, name: str) -> None:
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    path = FIG_DIR / name
    fig.savefig(path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"[SAVED] {path.relative_to(ROOT)}")


def footnote(fig: plt.Figure, text: str) -> None:
    fig.text(0.01, -0.02, text, fontsize=8, color=TEXT_2, ha="left", va="top")


def probability_difference_scatter(matches: pd.DataFrame) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(12, 3.8), sharey=True)
    x = matches["p_proportional_fav"]
    edges = np.linspace(0.5, 1.0, 26)
    centres = (edges[:-1] + edges[1:]) / 2
    for ax, method in zip(axes, ALTERNATIVES):
        diff = (matches[f"p_{method}_fav"] - x) * 100
        ax.scatter(x * 100, diff, s=2, alpha=0.08, color=COLORS[method], linewidths=0, rasterized=True)
        binned = diff.groupby(pd.cut(x, edges), observed=False).mean().to_numpy()
        ax.plot(centres * 100, binned, color=TEXT, lw=2, label="bin mean")
        ax.axhline(0, color=TEXT_2, lw=0.8)
        ax.set_title(f"{LABELS[method]} - Proportional", color=TEXT)
        ax.set_xlabel("Proportional favourite probability (%)")
        ax.set_xlim(50, 100)
    axes[0].set_ylabel("Favourite probability difference (pp)")
    axes[0].legend(loc="upper left")
    fig.suptitle("Alternative methods move probability towards the favourite", x=0.01, ha="left",
                 fontweight="bold")
    footnote(fig, f"Pinnacle closing odds, ATP+WTA 2019-2025, n = {len(matches):,} completed matches. "
                  "Each dot is one match; the underdog difference is the same with opposite sign.")
    fig.tight_layout()
    save(fig, "fig1_probability_difference_vs_proportional.png")


def probability_difference_by_asymmetry(table: pd.DataFrame) -> None:
    bins = list(dict.fromkeys(table["asymmetry_bin"]))
    order = ["1-1.5", "1.5-2", "2-3", "3-5", "5-10", "10+"]
    bins = [b for b in order if b in bins]
    width = 0.26
    x = np.arange(len(bins))
    fig, ax = plt.subplots(figsize=(9, 4.2))
    for i, method in enumerate(ALTERNATIVES):
        t = table[table["method"] == method].set_index("asymmetry_bin").loc[bins]
        mean = t["mean_diff"] * 100
        err = np.vstack([mean - t["p05_diff"] * 100, t["p95_diff"] * 100 - mean])
        ax.bar(x + (i - 1) * width, mean, width - 0.03, color=COLORS[method], label=LABELS[method],
               yerr=err, error_kw={"ecolor": TEXT_2, "elinewidth": 0.8, "capsize": 2})
    n = table.drop_duplicates("asymmetry_bin").set_index("asymmetry_bin").loc[bins, "n"]
    ax.set_xticks(x, [f"{b}\n(n={v:,})" for b, v in zip(bins, n)])
    ax.set_xlabel("Market asymmetry: underdog odds / favourite odds")
    ax.set_ylabel("Favourite probability vs Proportional (pp)")
    ax.set_title("The more lopsided the market, the more the methods disagree", loc="left")
    ax.legend(loc="upper left")
    ax.grid(axis="x", visible=False)
    footnote(fig, "Bars: mean difference; whiskers: 5th-95th percentile. Pinnacle closing odds, ATP+WTA 2019-2025.")
    fig.tight_layout()
    save(fig, "fig2_probability_difference_by_asymmetry.png")


def signal_counts(counts: pd.DataFrame) -> None:
    counts = counts.set_index("method").loc[METHODS]
    fig, ax = plt.subplots(figsize=(7, 3.8))
    bars = ax.bar([LABELS[m] for m in METHODS], counts["signals"], 0.6,
                  color=[COLORS[m] for m in METHODS])
    for bar, value in zip(bars, counts["signals"]):
        ax.text(bar.get_x() + bar.get_width() / 2, value, f"{value:,}", ha="center", va="bottom",
                color=TEXT, fontsize=9)
    ax.set_ylabel("Sides with EV >= threshold")
    min_ev = counts["min_ev"].iloc[0]
    ax.set_title(f"Historical EV signals (EV >= {min_ev:.0%}) by devig method", loc="left")
    ax.grid(axis="x", visible=False)
    footnote(fig, f"Pinnacle closing odds devigged, priced against Bet365 (tennis-data.co.uk), "
                  f"{int(counts['sides_evaluated'].iloc[0]):,} sides.")
    fig.tight_layout()
    save(fig, "fig3_signal_counts.png")


def signal_overlap(overlap: pd.DataFrame) -> None:
    jac = overlap.pivot(index="method_a", columns="method_b", values="jaccard").loc[METHODS, METHODS]
    both = overlap.pivot(index="method_a", columns="method_b", values="both").loc[METHODS, METHODS]
    cmap = LinearSegmentedColormap.from_list("seq_blue", BLUES)
    fig, ax = plt.subplots(figsize=(6, 5))
    im = ax.imshow(jac.to_numpy(), cmap=cmap, vmin=0, vmax=1)
    ax.grid(False)
    ax.set_xticks(range(4), [LABELS[m] for m in METHODS])
    ax.set_yticks(range(4), [LABELS[m] for m in METHODS])
    for i in range(4):
        for j in range(4):
            v = jac.iat[i, j]
            ax.text(j, i, f"{v:.2f}\n{int(both.iat[i, j]):,} shared", ha="center", va="center",
                    fontsize=8.5, color="white" if v > 0.6 else TEXT)
    for spine in ax.spines.values():
        spine.set_visible(False)
    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label("Jaccard overlap (shared / union)", color=TEXT_2)
    cbar.outline.set_visible(False)
    ax.set_title("Signal overlap between methods", loc="left")
    fig.tight_layout()
    save(fig, "fig4_signal_overlap.png")


def favourite_underdog(counts: pd.DataFrame) -> None:
    counts = counts.set_index("method").loc[METHODS]
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.8), sharey=True)
    for ax, column, title in zip(axes, ["favourite_signals", "underdog_signals"],
                                 ["Favourite signals", "Underdog signals"]):
        bars = ax.bar([LABELS[m] for m in METHODS], counts[column], 0.6,
                      color=[COLORS[m] for m in METHODS])
        for bar, value, total in zip(bars, counts[column], counts["signals"]):
            ax.text(bar.get_x() + bar.get_width() / 2, value, f"{value:,}\n({value / total:.0%})",
                    ha="center", va="bottom", color=TEXT, fontsize=8.5)
        ax.set_title(title, loc="left")
        ax.grid(axis="x", visible=False)
    axes[0].set_ylabel("Sides with EV >= threshold")
    axes[0].set_ylim(0, counts[["favourite_signals", "underdog_signals"]].to_numpy().max() * 1.18)
    fig.suptitle("Proportional flags far more underdogs; Power shifts signals to favourites",
                 x=0.01, ha="left", fontweight="bold")
    footnote(fig, "Percentages are the share of that method's signals. Favourite = shorter Pinnacle price.")
    fig.tight_layout()
    save(fig, "fig5_favourite_underdog_signals.png")


def calibration(cal: pd.DataFrame) -> None:
    cal = cal[cal["binning"] == "proportional"]
    bins = list(dict.fromkeys(cal["bin"]))
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.6))

    obs = cal.drop_duplicates("bin").set_index("bin").loc[bins]
    ax1.plot([50, 100], [50, 100], color=TEXT_2, lw=0.8, ls="--", label="perfect calibration")
    for method in METHODS:
        t = cal[cal["method"] == method].set_index("bin").loc[bins]
        ax1.plot(t["mean_predicted"] * 100, t["observed"] * 100, marker=MARKERS[method], ms=6,
                 lw=1.5, color=COLORS[method], label=LABELS[method])
    ax1.set_xlabel("Mean predicted favourite win probability (%)")
    ax1.set_ylabel("Observed favourite win rate (%)")
    ax1.set_title("Reliability", loc="left")
    ax1.legend(loc="upper left")

    x = np.arange(len(bins))
    offsets = np.linspace(-0.24, 0.24, len(METHODS))
    ax2.axhline(0, color=TEXT_2, lw=0.8)
    for off, method in zip(offsets, METHODS):
        t = cal[cal["method"] == method].set_index("bin").loc[bins]
        ax2.errorbar(x + off, t["observed_minus_predicted"] * 100, yerr=1.96 * t["se_observed"] * 100,
                     fmt=MARKERS[method], ms=6, color=COLORS[method], ecolor=COLORS[method],
                     elinewidth=1.2, capsize=2, label=LABELS[method])
    ax2.set_xticks(x, [f"{b}\n(n={n:,})" for b, n in zip(bins, obs["n"])])
    ax2.set_xlabel("Favourite probability bin (Proportional)")
    ax2.set_ylabel("Observed - predicted (pp)")
    ax2.set_title("Calibration error with 95% interval", loc="left")
    ax2.grid(axis="x", visible=False)
    ax2.legend(loc="upper left")
    fig.suptitle("Proportional underestimates strong favourites; Power comes closest",
                 x=0.01, ha="left", fontweight="bold")
    footnote(fig, "All methods are scored on the same matches per bin (bins on Proportional probability). "
                  "Pinnacle closing odds, ATP+WTA 2019-2025, completed matches, ties excluded.")
    fig.tight_layout()
    save(fig, "fig6_calibration.png")


def paired_log_loss(by_year: pd.DataFrame, overall: pd.DataFrame, overall_ret: pd.DataFrame) -> None:
    fig, ax = plt.subplots(figsize=(11, 4.6))
    groups = [("All\n(completed)", overall), ("All\n(+retirements)", overall_ret)]
    groups += [(str(y), by_year[by_year["year"] == y]) for y in sorted(by_year["year"].unique())]
    x = np.arange(len(groups))
    offsets = [-0.22, 0, 0.22]
    ax.axhline(0, color=TEXT_2, lw=0.8)
    for off, method in zip(offsets, ALTERNATIVES):
        mean, err = [], []
        for _, table in groups:
            row = table[(table["method"] == method)].iloc[0]
            mean.append(row["mean_diff"] * 1e4)
            err.append(1.96 * row["se"] * 1e4)
        ax.errorbar(x + off, mean, yerr=err, fmt=MARKERS[method], ms=6, color=COLORS[method],
                    elinewidth=1.2, capsize=2, label=LABELS[method])
    ax.axvline(1.5, color=GRID, lw=1)
    ax.set_xticks(x, [g for g, _ in groups])
    ax.set_ylabel("Log Loss difference vs Proportional\n(x 10^-4, below 0 = better)")
    ax.set_title("Paired Log Loss difference against Proportional, 95% intervals", loc="left")
    ax.grid(axis="x", visible=False)
    ax.legend(loc="lower right", ncol=3)
    footnote(fig, "Interval = mean +/- 1.96 x standard error of the per-match paired difference.")
    fig.tight_layout()
    save(fig, "fig7_paired_log_loss.png")


def main() -> None:
    matches = pd.read_csv(HIST / "matches_evaluated.csv")
    paired = pd.read_csv(HIST / "paired_log_loss.csv")
    paired_ret = pd.read_csv(HIST / "with_retirements_paired_log_loss.csv")
    counts = pd.read_csv(SIGNALS / "signal_counts.csv")

    probability_difference_scatter(matches)
    probability_difference_by_asymmetry(pd.read_csv(HIST / "probability_differences_by_asymmetry.csv"))
    signal_counts(counts)
    signal_overlap(pd.read_csv(SIGNALS / "signal_overlap.csv"))
    favourite_underdog(counts)
    calibration(pd.read_csv(HIST / "calibration.csv"))
    paired_log_loss(
        pd.read_csv(HIST / "paired_log_loss_by_year.csv"),
        paired[paired["reference"] == "proportional"],
        paired_ret[paired_ret["reference"] == "proportional"],
    )

    DOCS_FIG_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(FIG_DIR.glob("*.png")):
        shutil.copy2(path, DOCS_FIG_DIR / path.name)
    print(f"[COPIED] figures -> {DOCS_FIG_DIR.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
