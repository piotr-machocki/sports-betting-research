#!/usr/bin/env bash
# Daily off-site backup of data/ for cron on Linux (the VM is the only copy of the
# live data). Packs every file changed since the last successful backup into one
# data_<UTC time>.tar.xz and uploads it with rclone; the first run packs everything.
# A failure is logged to data/logs/backup.log and sent to Telegram (notify.py
# --backup-failed); the next run then includes everything this one missed.
#
# Only data that cannot be made again is kept (since 15 Nov 2026): odds, feeds, session
# CSVs and status, results, NBA snapshots, notify state. Left out (SKIP below): exports
# and reports (results_store.py --all rebuilds them from the session CSVs), the
# cumulative processed_odds.csv (the session CSVs joined), the Betclic queue messages
# (every field is in data/betclic/raw), timings, debug and failure dumps, the latest-only
# Pinnacle files, logs and caches. That took one day from 30 MB to 14 MB.
#
# .gz files go in unzipped and the archive is xz: runs 5 minutes apart are nearly the
# same, which gzip cannot see across files (the Superbet feeds of one day: 4.7 MB as .gz
# files, 0.2 MB this way). Their paths are listed in GUNZIPPED inside the archive.
#
# Restore: download every archive and extract them oldest first, gzipping each one's
# GUNZIPPED files back before the next (older archives are .tar.gz, without that list):
#   rclone copy gdrive:sports-betting-research/backups backups/
#   for f in $(ls backups/data_*.tar.* | sort); do
#       tar xf "$f"
#       if [ -f GUNZIPPED ]; then
#           while read -r g; do gzip -nf "${g%.gz}"; done < GUNZIPPED
#           rm GUNZIPPED
#       fi
#   done
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
name="data_$(date -u +%Y%m%d_%H%M%S).tar.xz"
newer=()
[ -f "$MARK" ] && newer=(-newer "$MARK")

SKIP='^data/(backup|cache|logs)/'
SKIP+='|^data/results/(raw|reports)/|^data/results/sessions/.*\.txt$|^data/results/processed_odds'
SKIP+='|^data/betclic/(processed_queue|incoming|debug|failures|timing)/'
SKIP+='|^data/pinnacle/(odds\.csv$|json/)|\.lock$'

find data -type f "${newer[@]}" > "$work/found" || fail "listing files failed"
grep -Ev "$SKIP" "$work/found" > "$work/files"
count=$(wc -l < "$work/files")

if [ "$count" -gt 0 ]; then
    # A copy to pack: hard links, and .gz files unzipped (listed in GUNZIPPED).
    stage="$work/stage"
    mkdir -p "$stage"
    : > "$stage/GUNZIPPED"
    while read -r f; do
        mkdir -p "$stage/$(dirname "$f")"
        case "$f" in
            *.gz)
                # One being written fails here; it changed after "started", so the
                # next backup has it.
                if gzip -dc "$f" > "$stage/${f%.gz}" 2>/dev/null; then
                    echo "$f" >> "$stage/GUNZIPPED"
                else
                    rm -f "$stage/${f%.gz}"
                    echo "skipped unreadable $f"
                fi ;;
            *) ln "$f" "$stage/$f" 2>/dev/null || cp -p "$f" "$stage/$f" ;;
        esac
    done < "$work/files"

    # Exit code 1 only means a file changed while it was read (a run writing):
    # the archive is still written and that file goes into the next backup too.
    (cd "$stage" && nice -n 10 tar cf - data GUNZIPPED) \
        | nice -n 10 xz -6 -T1 > "$work/$name"
    codes=("${PIPESTATUS[@]}")
    [ "${codes[0]}" -le 1 ] || fail "tar exited with code ${codes[0]}"
    [ "${codes[1]}" -eq 0 ] || fail "xz exited with code ${codes[1]}"
    rm -rf "$stage"

    rclone copy "$work/$name" "$REMOTE" --retries 3
    code=$?
    [ "$code" -eq 0 ] || fail "upload to $REMOTE failed (rclone exit code $code), see data/logs/backup.log"

    echo "uploaded $name: $count files, $(du -h "$work/$name" | cut -f1)"
else
    echo "nothing changed since the last backup"
fi

touch -r "$work/started" "$MARK"
hc_ping 0
