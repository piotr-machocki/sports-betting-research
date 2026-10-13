import pytest

from betting.margin import METHODS, remove_margin

CASES = [
    [1.90, 1.90],
    [1.50, 2.60],
    [1.05, 11.00],
    [1.01, 15.00],
]


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("odds", CASES)
def test_sums_to_one_and_keeps_order(method, odds):
    p = remove_margin(odds, method)
    assert sum(p) == pytest.approx(1.0, abs=1e-9)
    assert all(0 < x < 1 for x in p)
    assert (p[0] > p[1]) == (odds[0] < odds[1])  # shorter odds = higher probability


@pytest.mark.parametrize("method", METHODS)
def test_symmetric_market_is_fifty_fifty(method):
    assert remove_margin([1.90, 1.90], method) == pytest.approx([0.5, 0.5])


@pytest.mark.parametrize("method", ["power", "odds_ratio"])
def test_longshot_gets_more_margin_than_proportional(method):
    prop = remove_margin([1.05, 11.00], "proportional")
    alt = remove_margin([1.05, 11.00], method)
    assert alt[1] < prop[1]


def test_unknown_method():
    with pytest.raises(ValueError):
        remove_margin([1.9, 1.9], "nope")