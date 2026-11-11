#!/usr/bin/env bash
# Daily off-site backup of data/ for cron on Linux (the VM is the only copy of the
# live data). Packs every file changed since the last successful backup into one
# data_<UTC time>.tar.gz and uploads it with rclone; the first run packs everything.
# A failure is logged to data/logs/backup.log and sent to Telegram (notify.py
# --backup-failed); the next run then includes everything this one missed.
#
# Restore: download every archive and extract them oldest first, e.g.
#   rclone copy gdrive:sports-betting-research/backups backups/
#   for f in $(ls backups/*.tar.gz | sort); do tar xzf "$f"; done
#
# Remote: BACKUP_REMOTE (default gdrive:sports-betting-research/backups), an rclone
# remote set up once with `rclone config create gdrive drive scope=drive.file token=...`.
cd "$(dirname "$0")/.." || exit 1
REMOTE="${BACKUP_REMOTE:-gdrive:sports-betting-research/backups}"
MARK=data/backup/last_ok
mkdir -p data/logs data/backup
exec >> data/logs/backup.log 2>&1
echo "===== $(date -u '+%F %T') UTC ====="

# Heartbeat for an outside dead man's switch (e.g. healthchecks.io), optional:
# HEALTHCHECK_BACKUP_URL in .env gets /start now and /0 or /fail at the end, so the
# service also alerts when a backup hangs or cron stops running it.
hc_url=$(grep -m1 '^HEALTHCHECK_BACKUP_URL=' .env 2>/dev/null | cut -d= -f2- | tr -d "\r\"' ")
hc_ping() {
    [ -n "$hc_url" ] || return 0
    curl -fs -m 10 -o /dev/null "$hc_url/$1" || echo "healthcheck ping failed (curl exit $?)"
}
hc_ping start

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

fail() {
    echo "ERROR: $1"
    .venv/bin/python scripts/notify.py --backup-failed "$1"
    hc_ping fail
    exit 1
}

# Files changed from this moment on go into the next backup.
touch "$work/started"
name="data_$(date -u +%Y%m%d_%H%M%S).tar.gz"
newer=()
[ -f "$MARK" ] && newer=(-newer "$MARK")

find data -type f "${newer[@]}" \
    ! -path 'data/backup/*' ! -name '*.lock' ! -name 'backup.log' > "$work/files" \
    || fail "listing files failed"
count=$(wc -l < "$work/files")

if [ "$count" -gt 0 ]; then
    # Exit code 1 only means a file changed while it was read (a run writing):
    # the archive is still written and that file goes into the next backup too.
    nice -n 10 tar czf "$work/$name" -T "$work/files"
    code=$?
    [ "$code" -le 1 ] || fail "tar exited with code $code"

    rclone copy "$work/$name" "$REMOTE" --retries 3
    code=$?
    [ "$code" -eq 0 ] || fail "upload to $REMOTE failed (rclone exit code $code), see data/logs/backup.log"

    echo "uploaded $name: $count files, $(du -h "$work/$name" | cut -f1)"
else
    echo "nothing changed since the last backup"
fi

touch -r "$work/started" "$MARK"
hc_ping 0
