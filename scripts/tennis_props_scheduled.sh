#!/usr/bin/env bash
# One unattended tennis_props.py run, for cron ("2-59/15 * * * *": two minutes after a
# tennis run, whose session gives the book links, and clear of the NBA log at :07).
# Output goes to data/logs/tennis_props.log; flock skips a run while the previous one
# is still going. One PinnFeed request a run (shared key with the tennis runs).
cd "$(dirname "$0")/.." || exit 1
mkdir -p data/logs
exec 9>data/logs/tennis_props.lock
if ! flock -n 9; then
    echo "===== $(date -u '+%F %T') UTC: previous props run still going, skipped =====" >> data/logs/tennis_props.log
    exit 0
fi
echo "===== $(date -u '+%F %T') UTC =====" >> data/logs/tennis_props.log
nice -n 10 .venv/bin/python scripts/tennis_props.py "$@" >> data/logs/tennis_props.log 2>&1
echo "exit code $?" >> data/logs/tennis_props.log
