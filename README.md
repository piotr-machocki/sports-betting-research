# Sports Betting Research

Researching whether sharp-bookmaker prices can be used to identify positive-EV opportunities at soft bookmakers.

Collect odds from sharp bookmakers, remove the bookmaker margin, and calculate a reference probability. Compare this reference with odds from soft bookmakers and identify opportunities where the implied probability is sufficiently lower than the reference probability (e.g. EV > 2%).

The long-term goal is to develop an automated value-betting system with bankroll management and, potentially, arbitrage detection.

**Sharp:** Pinnacle, DIMMERS (later)

**Soft:** Betclic, SuperBet (later), Fortuna (later)

## Phase 1 - research / paper trading

Pinnacle → reference probabilities → Betclic comparison → identify positive-EV opportunities → notification → manual decision/bet → record result.

Only:

* sport: 🎾 Tennis
* sharp (reference): Pinnacle
* soft: Betclic 
* manual bet placement (no automation yet)
* prematch (no live betting yet)
* match winner (initially)
* stake: fixed 0.50 PLN (no Kelly)
* bet frequency: maximum ~1–2 bets/day
