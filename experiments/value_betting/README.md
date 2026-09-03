# README — Value Betting Reproduction Project (De-vig-First Methodology)

This is the build order. Each step produces a concrete artifact (a file, a table, a number) you need before moving to the next step. Do not skip the calibration steps (3 and 7) — they're what tells you whether the strategy has any right to exist on your data before you risk anything on backtested P&L numbers.

Two sports tracks, kept **fully separate** throughout: football (3-way market) and tennis (2-way market). Do not share constants, thresholds, or regressions between them.

---

## Step 0 — Project structure

```
value-betting/
├── data/
│   ├── raw/                # untouched downloaded odds/results files
│   └── processed/          # cleaned, joined tables (parquet/csv)
├── src/
│   ├── data_ingest.py      # download/load raw data per sport
│   ├── devig.py            # per-bookmaker margin removal
│   ├── consensus.py        # cross-bookmaker averaging
│   ├── calibration.py      # Figure-1-style regression + binning
│   ├── strategy.py         # bet-selection logic (Eq. 7 equivalent)
│   ├── backtest.py         # run strategy over historical data, produce P&L
│   ├── bootstrap.py        # random-bet null model + significance test
│   └── config.py           # sport-specific constants (kept apart!)
├── notebooks/               # exploratory analysis, plots
├── results/
│   ├── football/
│   └── tennis/
└── README.md                 # this file
```

Keep `results/football/` and `results/tennis/` fully separate — different calibration constants, different regression outputs, different backtest reports. Never merge them.

---

## Step 1 — Get the data

**Football**
- Source: football-data.co.uk (free, decimal odds, multiple bookmakers, includes several of the 32 named in the paper: Bet365, William Hill, Pinnacle, Betway, Ladbrokes). Download season-by-season CSVs for the leagues you want.
- Required columns per match: date, home team, away team, full-time result (H/D/A), and odds columns for each bookmaker × {H, D, A}.

**Tennis**
- Source: a historical tennis odds dataset with decimal 2-way odds per bookmaker (e.g., tennis-data.co.uk, which mirrors football-data.co.uk's format for ATP/WTA). Required columns: date, player A, player B, winner, tournament tier (Grand Slam / Masters / Challenger / WTA equivalent), and odds columns per bookmaker × {A win, B win}.

**Action:** write `data_ingest.py` to load and concatenate seasons into one clean table per sport in `data/processed/`, with a consistent schema:
```
match_id | date | league_or_tour_tier | side_A | side_B | result | bookmaker | odds_A | odds_D(football only) | odds_B
```

**Check before moving on:** confirm you have ≥3 bookmaker quotes for the large majority of matches (the paper's own minimum). If not, your dataset is too thin for a meaningful consensus.

---

## Step 2 — De-vig each bookmaker's quote

**Football (3-way), proportional method:**
```python
def devig_3way(odds_h, odds_d, odds_a):
    imp_h, imp_d, imp_a = 1/odds_h, 1/odds_d, 1/odds_a
    overround = imp_h + imp_d + imp_a
    return imp_h/overround, imp_d/overround, imp_a/overround
```

**Tennis (2-way), exact:**
```python
def devig_2way(odds_a, odds_b):
    imp_a, imp_b = 1/odds_a, 1/odds_b
    overround = imp_a + imp_b
    return imp_a/overround, imp_b/overround
```

**Action:** write `devig.py` with both functions. Apply row-by-row to every (match, bookmaker) quote. Store the de-vigged probabilities alongside the raw odds — you'll need both later (raw odds for execution price, de-vigged probability for the fair-value estimate).

**Sanity check:** de-vigged probabilities per match/bookmaker should sum to exactly 1.0 (or 1.000000 within floating point tolerance). If they don't, there's a bug.

---

## Step 3 — Build the consensus and run the calibration regression (do this BEFORE any backtest)

This is the step that tells you whether the whole approach is even viable on your data — do not skip it or jump straight to backtesting.

**3a. Consensus.** For each match/result, average the de-vigged probabilities across all bookmakers offering that match:
```python
p_cons = mean(devigged_probabilities_across_bookmakers)
```

**3b. Bin and check calibration (reproducing Figure 1, but on your de-vigged consensus).**
- Bin `p_cons` into ~80 bins from 0 to 1 (or fewer bins if your sample is smaller — keep ≥100 observations per bin as the paper does).
- For each bin, compute the realized frequency of that outcome actually occurring.
- Plot realized frequency vs. mean `p_cons` per bin. It should look close to a straight diagonal line (y = x) if de-vig alone is doing its job.

**3c. Regress.** Fit realized frequency on `p_cons`, separately for each outcome class (Home / Draw / Away for football; per-tier if you're splitting tennis):
```python
from sklearn.linear_model import LinearRegression
# realized_freq ~ p_cons per bin
```

**3d. Interpret the intercept — this is the decision point:**
- Intercept ≈ 0 (and slope ≈ 1) → de-vig alone is sufficient. Set your correction constant to 0 and use `p_true_estimate = p_cons` directly. No paper-style constant needed.
- Intercept meaningfully negative (matches typically underperform their de-vigged consensus probability, i.e., favorites are underpriced / longshots overpriced) → that's your evidence of residual favorite-longshot bias. Fit and keep this intercept as your correction constant, but note it's **your own fitted number**, not the paper's 0.05 — do not import theirs.

**Do this separately for:**
- Football: Home / Draw / Away, each gets its own intercept
- Tennis: run it split by tier (Grand Slam vs Masters/ATP500 vs Challenger, and separately for WTA) if you have enough data per tier; if not, at minimum run tennis completely separately from football

**Output:** `results/{sport}/calibration_report.json` with slope, intercept, R², and bin counts per outcome class. This is your single most important diagnostic — keep it and reference it whenever you report backtest results.

---

## Step 4 — Define the bet-selection rule

Now that you have a validated (or zero) correction constant per outcome class, define:

```python
def is_value_bet(devigged_consensus_p, correction_constant, max_raw_odds, num_bookmakers, min_bookmakers=3):
    if num_bookmakers < min_bookmakers:
        return False
    p_true = devigged_consensus_p - correction_constant
    if p_true <= 0:
        return False
    fair_odds = 1 / p_true
    return max_raw_odds > fair_odds
```

Note: compare against **raw odds** (`max_raw_odds`), not de-vigged odds — you're checking whether the actual tradeable price beats your fair-value estimate. The de-vig step is only used to build the consensus estimate, not the execution price.

**One-bet-per-match rule:** if multiple outcomes qualify simultaneously (rare, but possible), pick one and document the rule (e.g., highest edge = `max_raw_odds / fair_odds` ratio). Write this down explicitly in `strategy.py` — it's one of the ambiguities from the original paper you're now resolving deliberately instead of by accident.

---

## Step 5 — Backtest

```python
for match in dataset:
    for outcome in outcomes(match):   # {H,D,A} or {A,B}
        if is_value_bet(...):
            candidates.append(outcome)
    if candidates:
        chosen = select_one(candidates)   # your documented tie-break rule
        stake = FIXED_STAKE
        odds  = max_raw_odds_for(chosen)
        profit = stake*(odds-1) if match.result == chosen else -stake
        record(match, chosen, odds, profit)
```

Aggregate:
```
accuracy = wins / total_bets
total_profit = sum(profit)
yield_pct = total_profit / (stake * total_bets) * 100
```

**Output:** `results/{sport}/backtest_report.csv` — one row per bet, plus a summary table matching the paper's Table 1 layout (period, #bets, accuracy, profit, yield).

---

## Step 6 — Null model and significance test

```python
n_bets = len(your_placed_bets)
outcome_mix = empirical_distribution_of(your_placed_bets.chosen_outcome)  # e.g. {H:0.6, D:0.02, A:0.38}

null_returns = []
for i in range(2000):
    sample = random.sample(all_matches, n_bets, replace=True)
    total = 0
    for match in sample:
        random_outcome = weighted_choice(outcome_mix)
        odds = max_raw_odds_for(match, random_outcome)
        total += stake*(odds-1) if match.result == random_outcome else -stake
    null_returns.append(total)

sigma_separation = (your_total_profit - mean(null_returns)) / std(null_returns)
empirical_p = fraction(null_returns >= your_total_profit)
```

**Output:** `results/{sport}/significance_report.json` — mean/std of null distribution, your σ-separation, empirical p-value. Report this number honestly even if it's not impressive — a small or insignificant σ-separation is a valid and important finding, not a failure of the implementation.

---

## Step 7 — Compare de-vig-only vs. de-vig-plus-constant

Run Steps 4–6 twice per sport:
1. Using `correction_constant = 0` (pure de-vig)
2. Using your fitted intercept from Step 3

Compare accuracy, yield, and σ-separation between the two. This tells you directly whether the extra fitted correction is earning its keep or just adding an in-sample-fit risk without real benefit — always prefer the simpler model unless the more complex one clearly and robustly outperforms it.

**Better yet — do this with a train/test split**, not on the full dataset at once:
- Fit the Step 3 regression (and any correction constant) on, say, seasons/years 1 through N−2
- Backtest and run significance tests (Steps 5–6) only on the held-out seasons N−1, N
- This avoids the exact in-sample-fitting trap the original paper's α = 0.05 falls into

---

## Step 8 — Report

For each sport, produce a short report containing:
- Calibration regression results (slope, intercept, R², per outcome class/tier) — Step 3
- Whether you're using de-vig-only or de-vig-plus-constant, and why (Step 7 comparison)
- Backtest summary table (period, #bets, accuracy, yield) — train and test periods separately
- σ-separation and p-value vs. the random-bet null — train and test periods separately
- Explicit list of assumptions/tie-break rules you chose (from Step 4) — treat these as your own documented methodology, distinct from the original paper's

---

## Order of operations, summarized

```
1. Ingest data (football + tennis, separately)
2. De-vig every bookmaker quote (3-way vs 2-way formulas)
3. Build consensus, run calibration regression → decide constant per outcome class/tier
4. Define bet-selection rule using your validated constant
5. Backtest (train period)
6. Bootstrap null + significance test (train period)
7. Compare de-vig-only vs. de-vig-plus-constant; repeat 4–6 on held-out test period
8. Write up report per sport
```

Do not build any ML layer, staking optimization, or live scraping until Steps 1–8 are complete and reported for at least football. Tennis can run in parallel once you're confident the pipeline (not just the football-specific numbers) is correct.
