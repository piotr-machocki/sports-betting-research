import pytest

from betting.margin import METHODS, remove_margin
from betting.value import calculate_ev, ev_by_method


def test_positive_ev():
    assert calculate_ev(0.60, 1.75) == pytest.approx(0.05)


def test_zero_ev():
    assert calculate_ev(0.50, 2.00) == pytest.approx(0.0)


def test_negative_ev():
    assert calculate_ev(0.40, 2.00) == pytest.approx(-0.20)


def test_probability_must_be_between_zero_and_one():
    with pytest.raises(ValueError):
        calculate_ev(1.1, 2.00)


def test_odds_must_be_greater_than_one():
    with pytest.raises(ValueError):
        calculate_ev(0.50, 1.00)


def test_ev_by_method_covers_every_method_and_reuses_the_formulas():
    results = ev_by_method([1.50, 2.70], [1.55, 2.60])

    assert list(results) == list(METHODS)

    for method, (probabilities, evs) in results.items():
        expected = remove_margin([1.50, 2.70], method)
        assert probabilities == pytest.approx(expected)
        assert evs == pytest.approx(
            [calculate_ev(p, o) for p, o in zip(expected, [1.55, 2.60])]
        )


def test_ev_by_method_rejects_mismatched_markets():
    with pytest.raises(ValueError):
        ev_by_method([1.50, 2.70], [1.55])
