#!/usr/bin/env bash
# The daily crawl on the droplet: what the Mac's crontab line does (downloader.py,
# then analysis.py, then analysis_consumer.py, then metrics.py, then judgements.py, then dashboard_stats.py), run by case-progress-crawl.service.
# A failing step doesn't stop the later ones (the analyses still rebuild from whatever
# is in the database); the script exits non-zero if any step failed, so
# `systemctl status case-progress-crawl` shows it. Output goes to the journal:
#   journalctl -u case-progress-crawl
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."        # the crawler directory

PY=${PYTHON:-/opt/case_manager/.venv/bin/python}   # the web app's venv has everything the crawler needs
status=0
for step in downloader analysis analysis_consumer metrics judgements dashboard_stats; do
  echo "== $(date -u +%FT%TZ) $step"
  "$PY" "$step.py" || { echo "!! $step failed (exit $?)" >&2; status=1; }
done
echo "== $(date -u +%FT%TZ) done (status $status)"
exit $status
