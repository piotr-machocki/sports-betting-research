## Margin removal / devigging

The reference (baseline) method is **proportional normalization**, applied to two-way tennis match-winner markets.

For decimal odds $o_i$, the raw implied probability is:

$$
q_i = \frac{1}{o_i}
$$

The normalized probability is:

$$
p_i = \frac{q_i}{\sum_j q_j}
$$

For the two-way tennis market:

$$
p_A = \frac{1/o_A}{1/o_A + 1/o_B}
$$

$$
p_B = \frac{1/o_B}{1/o_A + 1/o_B}
$$

### Alternative methods (implemented)

Three alternative methods are implemented in `src/betting/margin.py`. They differ in how the overround is distributed across the outcomes. In each case the parameter is found numerically (Brent's method) so that $\sum_i p_i = 1$.

**Power** (parameter $k \ge 1$):

$$
p_i = q_i^{k}
$$

**Odds ratio** (parameter $c \ge 1$):

$$
p_i = \frac{q_i}{q_i + c\,(1 - q_i)}
$$

**Shin** (parameter $z \in (0,1)$, the estimated share of insider money, with $\pi = \sum_j q_j$):

$$
p_i = \frac{\sqrt{z^2 + 4(1-z)\,q_i^2/\pi} - z}{2(1-z)}
$$

If the market has no overround ($\sum_i q_i \le 1$), all methods fall back to proportional normalization.

### Current status

`scripts/compare_odds.py` computes the Pinnacle -> Betclic EV with all four methods (proportional as the reference, plus power, Shin and odds ratio) and reports them side by side.

What is **not** done yet is the empirical evaluation of whether the choice of method materially changes the EV signals. This remains the research question of the project. The logarithmic method and MPTO, mentioned in `docs/notes/devigging.md`, are not implemented yet.