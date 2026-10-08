## Margin removal / devigging

The initial implementation will use **proportional normalization** to remove the bookmaker margin from two-way tennis match-winner markets.

For decimal odds \(o_i\), the raw implied probability is:

$$
q_i = \frac{1}{o_i}
$$

The normalized probability is:

$$
p_i = \frac{q_i}{\sum_j q_j}
$$

For the initial two-way tennis market:

$$
p_A = \frac{1/o_A}{1/o_A + 1/o_B}
$$

$$
p_B = \frac{1/o_B}{1/o_A + 1/o_B}
$$

This will serve as the baseline devigging method for the research phase.

The baseline will later be compared with alternative approaches, particularly the **Power method** and **Shin's method**, to determine whether the choice of devigging method affects the resulting probabilities and EV signals.
