#!/usr/bin/env bash
# The daily crawl on the droplet: what the Mac's crontab line does (downloader.py,
# then analysis.py, then analysis_dmc.py, then analysis_consumer.py, then metrics.py, then judgements.py, then dashboard_stats.py), run by case-progress-crawl.service.
# A failing step doesn't stop the later ones (the analyses still rebuild from whatever
# is in the database); the script exits non-zero if any step failed, so
# `systemctl status case-progress-crawl` shows it. Output goes to the journal:
#   journalctl -u case-progress-crawl
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."        # the crawler directory

# One crawl at a time: a second run (the timer firing while a long one is still going) would fetch the same
# cases and write the same tables. It skips itself, exiting 0, rather than queueing. The lock file is in the
# crawler directory because the service can write nowhere else (ProtectSystem=strict); it is released when
# this script exits, however it exits. (Only runs through this script take the lock: a hand-started
# downloader.py does not, so don't start the timer's run on top of one.)
exec 9>.crawl.lock
if ! flock -n 9; then
  echo "== $(date -u +%FT%TZ) another crawl is already running; skipping this one"
  exit 0
fi

PY=${PYTHON:-/opt/case_manager/.venv/bin/python}   # the web app's venv has everything the crawler needs
status=0
for step in downloader analysis analysis_dmc analysis_consumer metrics judgements dashboard_stats; do
  echo "== $(date -u +%FT%TZ) $step"
  "$PY" "$step.py" || { echo "!! $step failed (exit $?)" >&2; status=1; }
done
echo "== $(date -u +%FT%TZ) done (status $status)"
exit $status
