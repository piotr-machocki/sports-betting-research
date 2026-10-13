"""Bookmaker margin removal (devigging).

Every method takes decimal odds and returns probabilities that sum to 1.
They differ in HOW the overround is spread across outcomes.
"""
from math import sqrt
from typing import Callable

from scipy.optimize import brentq

_EPS = 1e-12


def _proportional(q: list[float]) -> list[float]:
    total = sum(q)
    return [x / total for x in q]


def _power(q: list[float]) -> list[float]:
    """p_i = q_i ** k, with k >= 1 chosen so that sum(p) = 1."""
    def f(k: float) -> float:
        return sum(x ** k for x in q) - 1

    hi = 2.0
    while f(hi) > 0:  # f(1) > 0 (overround), f(k) -> -1 as k -> inf
        hi *= 2
    k = brentq(f, 1.0, hi)
    return [x ** k for x in q]


def _odds_ratio(q: list[float]) -> list[float]:
    """p_i = q_i / (q_i + c * (1 - q_i)), with c >= 1 chosen so that sum(p) = 1."""
    def probs(c: float) -> list[float]:
        return [x / (x + c * (1 - x)) for x in q]

    def f(c: float) -> float:
        return sum(probs(c)) - 1

    hi = 2.0
    while f(hi) > 0:
        hi *= 2
    c = brentq(f, 1.0, hi)
    return probs(c)


def _shin(q: list[float]) -> list[float]:
    """Shin (1992/1993): z = estimated share of insider money.

    p_i = (sqrt(z^2 + 4 (1 - z) q_i^2 / pi) - z) / (2 (1 - z)),  pi = sum(q),
    z in (0, 1) chosen so that sum(p) = 1.
    """
    pi = sum(q)

    def probs(z: float) -> list[float]:
        return [(sqrt(z * z + 4 * (1 - z) * x * x / pi) - z) / (2 * (1 - z)) for x in q]

    def f(z: float) -> float:
        return sum(probs(z)) - 1

    # f(0) = sqrt(pi) - 1 > 0; f(z -> 1) = sum(q^2)/pi - 1 < 0
    z = brentq(f, 0.0, 0.999999)
    p = probs(z)
    total = sum(p)  # remove solver residue
    return [x / total for x in p]


METHODS: dict[str, Callable[[list[float]], list[float]]] = {
    "proportional": _proportional,
    "power": _power,
    "shin": _shin,
    "odds_ratio": _odds_ratio,
}


def remove_margin(odds: list[float], method: str = "proportional") -> list[float]:
    """Convert bookmaker odds into implied probabilities without the margin."""
    if not odds:
        raise ValueError("Odds list cannot be empty.")

    if any(odd <= 1.0 for odd in odds):
        raise ValueError("Decimal odds must be greater than 1.0.")

    if method not in METHODS:
        raise ValueError(f"Unknown method {method!r}. Choose from {list(METHODS)}.")

    q = [1 / odd for odd in odds]

    # No overround (or an underround): there is no margin to redistribute,
    # the solvers above assume sum(q) > 1.
    if sum(q) <= 1 + _EPS:
        return _proportional(q)

    return METHODS[method](q)