#!/usr/bin/env bash
# One unattended nba_collect.py run, for cron (offset from the tennis run,
# e.g. "7-59/15 * * * *"). Output goes to data/logs/nba_collect.log; flock
# skips a run while the previous one is still going. It uses no PinnAPI
# request unless PINNAPI_KEY_NBA is set in .env.
cd "$(dirname "$0")/.." || exit 1
mkdir -p data/logs
exec 9>data/logs/nba_collect.lock
if ! flock -n 9; then
    echo "===== $(date -u '+%F %T') UTC: previous NBA run still going, skipped =====" >> data/logs/nba_collect.log
    exit 0
fi
echo "===== $(date -u '+%F %T') UTC =====" >> data/logs/nba_collect.log
nice -n 10 .venv/bin/python scripts/nba_collect.py "$@" >> data/logs/nba_collect.log 2>&1
echo "exit code $?" >> data/logs/nba_collect.log
