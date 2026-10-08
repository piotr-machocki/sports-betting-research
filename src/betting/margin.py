def remove_margin(odds: list[float]) -> list[float]:
    """Convert bookmaker odds into normalized implied probabilities."""
    if not odds:
        raise ValueError("Odds list cannot be empty.")

    if any(odd <= 1.0 for odd in odds):
        raise ValueError("Decimal odds must be greater than 1.0.")

    implied_probabilities = [1 / odd for odd in odds]
    total_probability = sum(implied_probabilities)

    return [
        probability / total_probability
        for probability in implied_probabilities
    ]