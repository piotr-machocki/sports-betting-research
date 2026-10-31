from betting.margin import METHODS, remove_margin


def calculate_ev(probability: float, odds: float) -> float:
    """Calculate expected value for a decimal-odds bet."""
    if not 0 <= probability <= 1:
        raise ValueError("Probability must be between 0 and 1.")

    if odds <= 1.0:
        raise ValueError("Decimal odds must be greater than 1.0.")

    return probability * odds - 1


def ev_by_method(
    fair_odds: list[float],
    bet_odds: list[float],
) -> dict[str, tuple[list[float], list[float]]]:
    """Fair probabilities and EVs for every devig method.

    `fair_odds` are devigged (e.g. Pinnacle), `bet_odds` are the odds actually
    offered (e.g. Betclic), in the same outcome order. Returns
    {method: (probabilities, evs)} for every method in margin.METHODS.
    """
    if len(fair_odds) != len(bet_odds):
        raise ValueError("fair_odds and bet_odds must have the same length.")

    results = {}

    for method in METHODS:
        probabilities = remove_margin(fair_odds, method)
        evs = [
            calculate_ev(probability, odds)
            for probability, odds in zip(probabilities, bet_odds)
        ]
        results[method] = (probabilities, evs)

    return results
