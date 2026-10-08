def calculate_ev(probability: float, odds: float) -> float:
    """Calculate expected value for a decimal-odds bet."""
    if not 0 <= probability <= 1:
        raise ValueError("Probability must be between 0 and 1.")

    if odds <= 1.0:
        raise ValueError("Decimal odds must be greater than 1.0.")

    return probability * odds - 1