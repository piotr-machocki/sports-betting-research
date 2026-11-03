# Setup

This guide explains how to set up the project from a fresh clone. The Betclic integration is not included in the public repository.

## Requirements

* Python 3.12 or newer (`python --version`)
* Git
* A PinnAPI key (available at [PinnAPI](https://pinnapi.com))

## 1. Clone the repository

```bash
git clone https://github.com/piotr-machocki/sports-betting-research.git
cd sports-betting-research
```

## 2. Create and activate a virtual environment

```bash
python -m venv .venv
```

Activate it:

* Windows (PowerShell): `.venv\Scripts\Activate.ps1`
* Windows (Command Prompt): `.venv\Scripts\activate.bat`
* macOS / Linux: `source .venv/bin/activate`

Your terminal prompt should now start with `(.venv)`.

## 3. Install dependencies

```bash
pip install -e .
playwright install chromium
```

## 4. Get a PinnAPI key

1. Go to [PinnAPI](https://pinnapi.com) and create an account.
2. Open your account dashboard and generate an API key.
3. Copy the key.

## 5. Configure environment variables

Create a `.env` file in the repository root with the following content:

```dotenv
PINNAPI_KEY=your_key_here
RAPIDAPI_KEY=your_rapidapi_key_here
```

Replace the placeholders with your actual API keys. Do not commit your `.env` file.

`RAPIDAPI_KEY` is optional: it fetches finished match results from the [Tennis API - ATP WTA ITF](https://rapidapi.com/jjrm365-kIFr3Nx_odV/api/tennis-api-atp-wta-itf) on RapidAPI (the free plan allows 50 requests a day). Without it, a run only skips that step.

## 6. Add the Betclic integration

The Betclic integration is not included in the public repository. If you have been provided with `betclic.py`, place it at:

```text
src/betting/books/betclic.py
```

## 7. Run the pipeline

```bash
python scripts/run_once.py          # headless Betclic browser 
python scripts/run_once.py --head   # show the browser, for debugging
python scripts/run_once.py --debug  # save HTML + screenshot of failed Betclic matches
python scripts/run_once.py --no-monitor  # open no monitor terminal, for unattended runs
python scripts/run_once.py --no-superbet  # skip the Superbet collection (only runs if superbet.py is present)
```

The Betclic collector runs in tournament mode (`betclic_scroll.py`) when
`src/betting/books/betclic_scroll.py` is present, and otherwise uses `betclic.py`.
Other modes: `--hybrid` (`betclic_scroll.py --hybrid`), `--search` (`betclic.py`),
`--scroll` (`betclic_scroll.py --overview`) and `--feed` (`betclic_feed.py`).

Debug dumps (in `data/betclic/debug/`) are off by default because they slow
down every failed match; use `--debug` when the scraper stops finding matches.
Closing the browser window in `--head` mode stops the run; it is not reopened.

The pipeline runs three steps in sequence:

1. Betclic odds collection
2. Pinnacle odds collection
3. Odds comparison

When collecting Betclic odds, a Chromium browser window may open. Keep it open until the collection finishes.

## 8. Results

Every run saves its results under `data/results/`:

* `raw/<session_id>.csv` and `.json`: every observation with both bookmakers' odds, margins, fair probabilities and EV for all four devig methods (power, the reference method used for ranking and signals, then odds ratio, Shin and proportional), matching status, confidence and timestamps, plus Pinnacle's event ID (`pinnacle_event_id`, links one match across sessions) and scheduled start (`pinnacle_starts_at`, UTC). Both are blank for sessions recorded before they were added. Use these files for analysis.
* `reports/<session_id>.txt`: the final terminal table as plain text (all matches, all EV columns). `reports/latest.txt` is always the newest one.
* `sessions/`: the pipeline's working files for each session (session CSV, status JSON, full report with observation details).

Finished match results are stored separately under `data/match_results/` and joined to the odds only when analysing:

* `results.csv`: one row per finished match (winner, loser, score, `result_type`: completed, retired or walkover).
* `raw/<date>_<tour>.json`: the provider's rows per day.
* `state.json`: the last day fully fetched.

`run_once.py` updates them at the end of a run. It makes no request while yesterday (UTC) is already covered, so it fetches at most once a day, however many runs you make; use `--no-results` to skip the step. After days without a run it catches up from the last covered day. To run it by hand or measure how many Pinnacle matches have a result:

```bash
python scripts/match_results.py                    # same step as run_once.py
python scripts/match_results.py --from 2026-10-01  # fetch further back
python scripts/match_results.py --report           # coverage, makes no requests
```

To list every positive-EV opportunity found so far, with how both books moved afterwards, closing line value, agreement between the devig methods and the result (local files only, no requests; all columns go to `data/results/ev_evaluation/opportunities.csv`):

```bash
python scripts/evaluate_ev.py                # all opportunities
python scripts/evaluate_ev.py --min-ev 0.02  # only EV of at least 2%
python scripts/evaluate_ev.py --from 2026-11-03
```

To regenerate the raw data and reports for past sessions:

```bash
python scripts/results_store.py --all
```


