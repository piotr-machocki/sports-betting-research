"""Scoring helpers for evaluating devigged probabilities against outcomes.

All functions work on numpy arrays so they can be applied to tens of thousands
of matches at once. `p` is always the probability assigned to the outcome that
is scored with `y` (1 = it happened, 0 = it did not).
"""
import numpy as np

_CLIP = 1e-15


def log_loss(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Per-observation binary log loss (natural log)."""
    p = np.clip(np.asarray(p, dtype=float), _CLIP, 1 - _CLIP)
    y = np.asarray(y, dtype=float)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def brier(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Per-observation binary Brier score."""
    p = np.asarray(p, dtype=float)
    y = np.asarray(y, dtype=float)
    return (p - y) ** 2


def paired_difference(
    a: np.ndarray,
    b: np.ndarray,
    clusters: np.ndarray | None = None,
) -> dict[str, float]:
    """Mean of a - b with its standard error.

    `se` treats observations as independent. If `clusters` is given,
    `se_cluster` is the cluster-robust standard error (CR1 small-sample
    correction), which allows observations in the same cluster
    (e.g. the same tournament) to be correlated.
    """
    d = np.asarray(a, dtype=float) - np.asarray(b, dtype=float)
    n = len(d)
    if n < 2:
        raise ValueError("Need at least two observations.")

    mean = float(d.mean())
    se = float(d.std(ddof=1) / np.sqrt(n))
    result = {
        "n": n,
        "mean_diff": mean,
        "sd_diff": float(d.std(ddof=1)),
        "se": se,
        "t": mean / se if se > 0 else float("nan"),
        "ci95_low": mean - 1.96 * se,
        "ci95_high": mean + 1.96 * se,
        "share_a_better": float((d < 0).mean()),
    }

    if clusters is not None:
        clusters = np.asarray(clusters)
        # Sum of centred differences within each cluster.
        _, inverse = np.unique(clusters, return_inverse=True)
        sums = np.bincount(inverse, weights=d - mean)
        g = len(sums)
        se_cluster = float(np.sqrt(g / (g - 1) * np.sum(sums ** 2)) / n) if g > 1 else float("nan")
        result["n_clusters"] = g
        result["se_cluster"] = se_cluster
        result["t_cluster"] = mean / se_cluster if se_cluster > 0 else float("nan")

    return result


def calibration_bins(
    p: np.ndarray,
    y: np.ndarray,
    edges: list[float],
    bin_by: np.ndarray | None = None,
) -> list[dict[str, float]]:
    """Mean predicted vs observed frequency inside probability bins.

    Bins are [edge_i, edge_i+1), the last one closed. By default observations
    are binned by `p` itself; pass `bin_by` to bin all methods on a common
    probability (so every method is scored on the same matches per bin).
    """
    p = np.asarray(p, dtype=float)
    y = np.asarray(y, dtype=float)
    key = p if bin_by is None else np.asarray(bin_by, dtype=float)

    rows = []
    for i, (lo, hi) in enumerate(zip(edges[:-1], edges[1:])):
        last = i == len(edges) - 2
        mask = (key >= lo) & ((key <= hi) if last else (key < hi))
        n = int(mask.sum())
        if n == 0:
            rows.append({"bin_low": lo, "bin_high": hi, "n": 0})
            continue

        predicted = float(p[mask].mean())
        observed = float(y[mask].mean())
        se_observed = float(np.sqrt(observed * (1 - observed) / n))
        rows.append({
            "bin_low": lo,
            "bin_high": hi,
            "n": n,
            "mean_predicted": predicted,
            "observed": observed,
            "se_observed": se_observed,
            "observed_minus_predicted": observed - predicted,
            "z": (observed - predicted) / se_observed if se_observed > 0 else float("nan"),
        })

    return rows
