import pytest

from src.betting.value import calculate_ev


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