## Proportional normalization (Multiplicative method)

Proportional normalization is the baseline devigging method used in this project. It assumes that the bookmaker's overround is distributed proportionally across the implied probabilities.

For decimal odds \(o_i\), the raw implied probability is:

$$
q_i = \frac{1}{o_i}
$$

The devigged probability is then:

$$
p_i = \frac{q_i}{\sum_j q_j}
$$

For a two-way market, the resulting probabilities sum to 1.

### Why start with it?

Proportional normalization is simple, transparent, and provides a useful baseline for comparing more complex devigging models.

Pinnacle is also known for operating with relatively low margins on major markets. When the overround is small, different reasonable margin-removal methods can produce relatively similar estimates, particularly when the market is not highly asymmetric.

However, proportional normalization makes a strong assumption: that the bookmaker's margin is distributed proportionally across the outcomes. There is no reason to assume that this must always be true.

## Favorite-longshot bias

One reason to test alternative methods is the possibility of a favorite-longshot bias (FLB).

If the bookmaker's margin is not distributed proportionally, proportional normalization can produce systematically different estimates for favorites and longshots. This becomes particularly relevant when the probabilities of the outcomes are highly asymmetric.

For example, consider a tennis match priced at approximately 1.05 versus 11.00. The proportional method distributes the overround according to the implied probabilities, but another devigging model may assign a different share of the margin to the favorite and the longshot.

Rather than assuming in advance which method is most accurate, this project will test whether these differences are meaningful for the betting strategy.

## Alternative approaches

| Method                                          | Role                                      |
| ----------------------------------------------- | ----------------------------------------- |
| **Multiplicative / proportional normalization** | Baseline                                  |
| **Power method**                                | Alternative                               |
| **Shin method**                                 | Alternative                               |
| **Odds Ratio / MPTO**                           | Potentially important for Pinnacle tennis |
| **Logarithmic method**                          | Additional comparison                     |

The Power and Shin methods provide alternative assumptions about how the bookmaker's margin is distributed. Odds Ratio and MPTO are particularly interesting to investigate because they have been studied in the context of estimating true probabilities from bookmaker odds, including tennis markets.

## Testing

The project will initially use proportional normalization to calculate the reference probabilities from Pinnacle.

The alternative methods will then be implemented and compared to determine whether the choice of devigging model materially changes the resulting probabilities and EV signals when comparing Pinnacle with Betclic.

The key research question is:

> **Does the choice of devigging method materially change the EV opportunities identified by the Pinnacle → Betclic comparison?**

Rather than assuming that one method is universally superior, the project will evaluate their differences empirically.
