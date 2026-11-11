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
PINNAPI_KEY_TENNIS=your_key_here
RAPIDAPI_KEY=your_rapidapi_key_here
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
HEALTHCHECK_URL=
HEALTHCHECK_BACKUP_URL=
PINNAPI_KEY_NBA=
```

Replace the placeholders with your actual API keys. Do not commit your `.env` file. (`PINNAPI_KEY_TENNIS` was called `PINNAPI_KEY` before 12 Nov 2026; the old name is still read.)

`RAPIDAPI_KEY` is optional: it fetches finished match results from the [Tennis API - ATP WTA ITF](https://rapidapi.com/jjrm365-kIFr3Nx_odV/api/tennis-api-atp-wta-itf) on RapidAPI (the free plan allows 50 requests a day). Without it, a run only skips that step.

`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are optional: with them, each run sends a Telegram message for every new prematch bet at or above `--min-ev` (each match, book and side once; see `scripts/notify.py`). Create a bot with [@BotFather](https://t.me/BotFather), send it any message, then `python scripts/notify.py --chat-id` prints the chat ID and `python scripts/notify.py --test` checks the setup. `--no-notify` skips it for one run.

`HEALTHCHECK_URL` is optional and used only by `scripts/scheduled_run.sh`: after every run it pings that URL with the run's exit code (`<url>/<code>`), so a dead man's switch service such as [healthchecks.io](https://healthchecks.io) can alert you when pings stop (machine down, cron broken) or a run fails. Keep it secret: anyone with it can send pings. `HEALTHCHECK_BACKUP_URL` does the same for `scripts/backup_data.sh` (`/start` when it begins, `/0` or `/fail` at the end, so a hung backup alerts too).

`PINNAPI_KEY_NBA` is optional and used only by the NBA price log (section 9): a second PinnAPI key (from a second PinnAPI account), so its requests never use the tennis key's daily quota.

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
python scripts/run_once.py --monitor  # also open the live monitor terminal; closing it stops the run
python scripts/run_once.py --no-superbet  # skip the Superbet collection (only runs if superbet.py is present)
```

To follow a run in the browser, with filters:

```bash
pip install -e ".[dashboard]"
python scripts/dashboard.py   # http://localhost:8501; leave the one-time email prompt blank; stops about 10 s after the last tab closes
```

It only reads the session files, so it can stay open across runs. "Latest" follows the newest session and shows its matches as they are written.

The Betclic collector is `betclic_feed.py` when it and `betclic_scroll.py` are present
in `src/betting/books/`, then `betclic_scroll.py`, and otherwise `betclic.py`.
Other modes: `--tournaments` (`betclic_scroll.py`), `--hybrid` (`betclic_scroll.py --hybrid`),
`--search` (`betclic.py`) and `--scroll` (`betclic_scroll.py --overview`).

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

## 9. NBA price log (data collection only)

`scripts/nba_collect.py` saves one snapshot of NBA prices per run, for a later study of which reference price and devig method has an edge. It compares nothing and sends nothing. Each source is fetched once per run, with no retries; a source that fails is only logged:

* Pinnacle through PinnAPI (basketball), only with `PINNAPI_KEY_NBA` set.
* Action Network's public odds JSON: US sportsbooks (DraftKings, FanDuel, BetMGM, Caesars, bet365, BetRivers, Fanatics, Unibet, theScore), their consensus and the opening line. It refused requests from a Polish home IP and answered from a server in Sweden.
* ESPN's scoreboard: DraftKings' moneyline (opening and current) and final scores.
* Kalshi and Polymarket public market data: moneyline, spreads, totals and player props, as bid/ask probabilities.
* Player props of US books, in the hourly props run only: DraftKings from ESPN's API (over/under and milestones, with opening prices), and bet365, BetMGM, DraftKings, Caesars, FanDuel, Hard Rock, Fanatics and Kalshi (plus PrizePicks, Sleeper and Underdog lines) from scoresandodds.com, one page per player (about 80, 0.3 s apart).
* Superbet, through a private collector (`src/betting/books/superbet_nba.py`), run only if that file exists.

```bash
python scripts/nba_collect.py                       # every source; props in the first run of each hour
python scripts/nba_collect.py --sources kalshi,espn --props never --dry-run
python scripts/nba_collect.py --sources espn_props,scoresandodds --props always --dry-run
bash scripts/nba_scheduled.sh                       # for cron, e.g. "7-59/15 * * * *"
```

Outputs under `data/nba/`:

* `snapshots/<UTC date>/<run_id>.csv.gz`: one row per outcome. Columns: `source`, `book`, `game_key` (`<US Eastern date>_<AWAY>@<HOME>`), `starts_at`, `market` (moneyline, spread, total, player_points, ...), `side` (team code, over or under), `player`, `line` (the side's handicap, or the total), `price` (decimal; sportsbooks), and `bid`/`ask`/`last` (probabilities; exchanges). Kalshi props are one-way "N+" contracts, stored as over N - 0.5. Superbet prices are before its 12% tax. Games that have started are left out.
* `results/espn_results.csv`: final scores by `game_key`.
* `runs.jsonl`: rows, seconds and any error per source for each run.
* `espn_athletes.json`: ESPN athlete IDs and names, so each player is looked up once.
