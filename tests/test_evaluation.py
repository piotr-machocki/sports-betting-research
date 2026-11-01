import math

import numpy as np
import pytest

from betting.evaluation import brier, calibration_bins, log_loss, paired_difference


def test_log_loss_and_brier_known_values():
    p = np.array([0.8, 0.8])
    y = np.array([1, 0])
    assert log_loss(p, y) == pytest.approx([-math.log(0.8), -math.log(0.2)])
    assert brier(p, y) == pytest.approx([0.04, 0.64])


def test_log_loss_is_finite_at_extremes():
    assert np.isfinite(log_loss(np.array([0.0, 1.0]), np.array([1, 0]))).all()


def test_paired_difference_matches_manual_calculation():
    a = np.array([1.0, 2.0, 3.0, 4.0])
    b = np.array([1.5, 2.0, 2.0, 5.0])
    d = a - b
    result = paired_difference(a, b)
    assert result["mean_diff"] == pytest.approx(d.mean())
    assert result["se"] == pytest.approx(d.std(ddof=1) / 2)
    assert result["share_a_better"] == pytest.approx(0.5)


def test_cluster_se_with_singleton_clusters_is_close_to_iid():
    rng = np.random.default_rng(0)
    a, b = rng.normal(size=500), rng.normal(size=500)
    result = paired_difference(a, b, clusters=np.arange(500))
    # With one observation per cluster CR1 differs from the iid SE only by
    # the degrees-of-freedom factor.
    assert result["se_cluster"] == pytest.approx(result["se"], rel=0.01)


def test_calibration_bins_counts_and_closed_last_bin():
    p = np.array([0.55, 0.65, 0.95, 1.0])
    y = np.array([1, 0, 1, 1])
    rows = calibration_bins(p, y, [0.5, 0.6, 0.7, 0.9, 1.0])
    assert [r["n"] for r in rows] == [1, 1, 0, 2]
    assert rows[3]["observed"] == 1.0
    assert rows[3]["mean_predicted"] == pytest.approx(0.975)


def test_calibration_bins_can_bin_on_another_probability():
    p = np.array([0.58, 0.62])
    key = np.array([0.55, 0.55])
    rows = calibration_bins(p, np.array([1, 0]), [0.5, 0.6, 0.7], bin_by=key)
    assert rows[0]["n"] == 2
    assert rows[0]["mean_predicted"] == pytest.approx(0.60)
