import pytest

from src.betting.margin import remove_margin


def test_remove_margin_two_way_market():
    probabilities = remove_margin([1.60, 2.40])

    assert probabilities == pytest.approx([0.60, 0.40])


def test_remove_margin_probabilities_sum_to_one():
    probabilities = remove_margin([1.50, 2.50])

    assert sum(probabilities) == pytest.approx(1.0)


def test_remove_margin_rejects_empty_odds():
    with pytest.raises(ValueError):
        remove_margin([])


def test_remove_margin_rejects_invalid_odds():
    with pytest.raises(ValueError):
        remove_margin([1.50, 1.0])