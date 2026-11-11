# The crawl on the droplet

The case manager's `deploy/deploy.sh` copies this folder to
`/opt/case_progress_crawler/deploy/`, and `remote-deploy.sh` installs poppler, the systemd units
`case-progress-crawl.service` and `.timer`, and enables the timer. Nothing else to set up beyond
what the case manager's `deploy/README.md` ("Case tracker") already asks for.

- **What runs:** `run-crawl.sh` -- `downloader.py` (KMC hearing PDFs, consumer cause lists and case
  details), then `analysis.py` (needs poppler's `pdftotext`, which the deploy installs), then
  `analysis_consumer.py` -- as the `case-manager` user, using the web app's venv.
- **When:** every day at 14:00 India time (`case-progress-crawl.timer`; a run missed while the droplet was off is made
  up when it next boots).
- **Where it writes:** the `case_progress` database (`CASE_PROGRESS_DATABASE_URL`), and `pdfs/` and
  `case_details/` under `/opt/case_progress_crawler` -- the same folders the viewer at `/case_tracker/`
  reads. The viewer notices new data without a restart.
- **Run it now:** `sudo systemctl start --no-block case-progress-crawl`
- **See what happened:** `systemctl list-timers case-progress-crawl.timer`,
  `systemctl status case-progress-crawl`, `journalctl -u case-progress-crawl -e`

Run it in one place only: a second crawler (e.g. the Mac's crontab line) fills a different
database, and the two drift apart.
