#!/usr/bin/env bash
# One unattended run_once.py session, for cron on Linux (the counterpart of
# scheduled_run.cmd). Output is appended to data/logs/scheduled_run.log.
# flock skips a run while the previous one is still going, so two sessions
# never share the queue folder.
cd "$(dirname "$0")/.." || exit 1
mkdir -p data/logs
exec 9>data/logs/scheduled_run.lock
if ! flock -n 9; then
    echo "===== $(date -u '+%F %T') UTC: previous run still going, skipped =====" >> data/logs/scheduled_run.log
    exit 0
fi
echo "===== $(date -u '+%F %T') UTC =====" >> data/logs/scheduled_run.log
started=$(date +%s)
.venv/bin/python scripts/run_once.py "$@" >> data/logs/scheduled_run.log 2>&1
code=$?
echo "exit code $code" >> data/logs/scheduled_run.log
# Counts failed runs in a row and sends one Telegram alert after 3, at most once a day.
.venv/bin/python scripts/notify.py --after-run "$code" --run-started "$started" >> data/logs/scheduled_run.log 2>&1
# Heartbeat for an outside dead man's switch (e.g. healthchecks.io): pings
# HEALTHCHECK_URL from .env after every run, always as a success. The service
# alerts when pings stop, which this machine cannot report itself (stopped or
# deleted VM, broken cron). Failed runs are notify.py's job above (3 in a row):
# sending the exit code made a single failed run alert at once.
# One try, 10 s; the URL is never logged.
hc_url=$(grep -m1 '^HEALTHCHECK_URL=' .env 2>/dev/null | cut -d= -f2- | tr -d "\r\"' ")
if [ -n "$hc_url" ]; then
    curl -fs -m 10 -o /dev/null "$hc_url" || echo "healthcheck ping failed (curl exit $?)" >> data/logs/scheduled_run.log
fi
# If this run sent Telegram alerts, read those bets' prices again about 2 minutes after
# them (scripts/recheck_signals.py; nothing without an alert). Still under the lock, so
# the next run (5 minutes after this one started) cannot overlap it.
if [ "$code" -eq 0 ]; then
    .venv/bin/python scripts/recheck_signals.py >> data/logs/scheduled_run.log 2>&1
fi
