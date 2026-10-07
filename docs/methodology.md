## Margin removal / devigging

The initial implementation will use **proportional normalization** to remove the bookmaker margin from two-way tennis match-winner markets.

For decimal odds ($o_i$), the raw implied probability is:

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

The methodology will later be compared against alternative approaches, particularly the **power method** and **Shin's method**.
