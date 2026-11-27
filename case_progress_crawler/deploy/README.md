# The crawl on the droplet

The case manager's `deploy/deploy.sh` copies this folder to
`/opt/case_progress_crawler/deploy/`, and `remote-deploy.sh` installs poppler, the systemd units
`case-progress-crawl.service` and `.timer`, and enables the timer. Nothing else to set up beyond
what the case manager's `deploy/README.md` ("Case tracker") already asks for.

- **What runs:** `run-crawl.sh` -- `downloader.py` (KMC hearing PDFs, consumer cause lists and case
  details), then `analysis.py` (needs poppler's `pdftotext`, which the deploy installs), then `analysis_dmc.py` (the Delhi Medical Council's complaint registers, recovered from the Wayback Machine by `downloader.py --court dmc`), then
  `analysis_consumer.py`, then `metrics.py` (fills `case_metrics` and `hearing_metrics`), then `judgements.py` (classifies new judgement PDFs into `case_judgements`), then `dashboard_stats.py` (rebuilds the `view_*` tables the tracker's pages read, and stores the dashboards' figures in `dashboard_stats_by_state` and `analytics_pages`) -- as the `case-manager` user, using the web app's venv.
- **When:** every day at 10 pm India time (`case-progress-crawl.timer`; a run missed while the droplet was off is made
  up when it next boots).
- **Where it writes:** the `case_progress` database (`CASE_PROGRESS_DATABASE_URL`), and `pdfs/` and
  `case_details/` under `/opt/case_progress_crawler` -- the same folders the viewer at `/case_tracker/`
  reads. The viewer notices new data without a restart.
- **Order files:** each distinct order PDF or HTML page is kept once, in `case_details/store/`, and the `case_orders`
  table says which file each case's orders are. The files are named by their content, so without that table nothing says
  which case a file belongs to. Each deploy backs up the database to `/var/backups/case-manager/case_progress/` (the
  newest 5 are kept; restore one with `pg_restore`). That is on the droplet's own disk and as old as the last deploy:
  orders the crawl saved since then are not in it. The deploy moves files saved the old way
  (`case_details/consumer/<case>/orders/`) into the store but keeps the old ones; once the deployed tracker shows
  its orders, delete them to free the space (not while a crawl is running):
  `sudo bash -c 'cd /opt/case_progress_crawler; set -a; . /etc/case-manager/case-manager.env; set +a; runuser -u case-manager -- /opt/case_manager/.venv/bin/python migrate_orders_to_store.py --remove-originals'`
- **Run it now:** `sudo systemctl start --no-block case-progress-crawl`
- **See what happened:** `systemctl list-timers case-progress-crawl.timer`,
  `systemctl status case-progress-crawl`, `journalctl -u case-progress-crawl -e`

Run it in one place only: a second crawler (e.g. the Mac's crontab line) fills a different
database, and the two drift apart.
