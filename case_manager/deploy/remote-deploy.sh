#!/usr/bin/env bash
# The droplet's half of deploy.sh: deploy.sh copies the code (this file
# included) to $APP_DIR, then runs this there as root.
set -euo pipefail
export LC_ALL=C.UTF-8

APP_DIR=${APP_DIR:-/opt/case_manager}
CRAWLER_DIR=${CRAWLER_DIR:-/opt/case_progress_crawler}
ENV_FILE=/etc/case-manager/case-manager.env
BACKUP_DIR=/var/backups/case-manager
SERVICE=case-manager
# Migrations up to and including this one were applied by hand before
# schema_migrations existed; the first run records them as done.
BASELINE=26

cd "$APP_DIR"
chown -R -h case-manager:case-manager .
[ -d "$CRAWLER_DIR" ] && chown -R -h case-manager:case-manager "$CRAWLER_DIR"

echo "--- Python dependencies"
sudo -u case-manager .venv/bin/pip install --quiet -r requirements.txt

# Keep the systemd unit in step with the repo (it isn't covered by rsync).
if ! cmp -s deploy/case-manager.service /etc/systemd/system/$SERVICE.service; then
  cp deploy/case-manager.service /etc/systemd/system/$SERVICE.service
  systemctl daemon-reload
  echo "updated the systemd unit"
fi

# Runs psql as the app's own database role (from DATABASE_URL), so anything a
# migration creates is owned by it, exactly as when applied by hand.
app_psql() { (set -a; . "$ENV_FILE"; set +a; psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q "$@"); }

echo "--- Migrations"
if [ "$(app_psql -Atc "SELECT to_regclass('schema_migrations') IS NOT NULL")" = f ]; then
  app_psql -c "CREATE TABLE schema_migrations (filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
  for f in db/migrations/*.sql; do
    n=$(basename "$f")
    if [ $((10#${n%%_*})) -le $BASELINE ]; then
      app_psql -c "INSERT INTO schema_migrations (filename) VALUES ('$n')"
    fi
  done
  echo "created schema_migrations, baseline through $BASELINE"
fi

pending=()
for f in db/migrations/*.sql; do
  n=$(basename "$f")
  if [ "$(app_psql -Atc "SELECT count(*) FROM schema_migrations WHERE filename = '$n'")" = 0 ]; then
    pending+=("$f")
  fi
done

if [ ${#pending[@]} -gt 0 ]; then
  install -d -m 700 "$BACKUP_DIR"
  backup="$BACKUP_DIR/case_manager-$(date +%Y%m%d-%H%M%S).dump"
  sudo -u postgres pg_dump -Fc case_manager > "$backup"
  chmod 600 "$backup"
  echo "backed up the database to $backup"
  ls -1t "$BACKUP_DIR"/*.dump | tail -n +11 | xargs -r rm -f   # keep the newest 10
  for f in "${pending[@]}"; do
    echo "applying $f"
    app_psql -f "$f"
    app_psql -c "INSERT INTO schema_migrations (filename) VALUES ('$(basename "$f")')"
  done
else
  echo "no new migrations"
fi

# The case tracker (app.py mounts it at /case_tracker/) keeps its data in its own
# database, named by CASE_PROGRESS_DATABASE_URL in the env file. Creating that
# database and its role is a one-off by hand (like case_manager's); this only
# makes sure its tables exist (idempotent). A problem here warns -- it doesn't stop the deploy.
echo "--- Case tracker database"
if [ ! -d "$CRAWLER_DIR" ]; then
  echo "no $CRAWLER_DIR: the case tracker is not mounted"
elif ! grep -q '^CASE_PROGRESS_DATABASE_URL=' "$ENV_FILE"; then
  echo "WARNING: CASE_PROGRESS_DATABASE_URL isn't in $ENV_FILE, so the case tracker has no database yet" >&2
else
  (set -a; . "$ENV_FILE"; set +a; cd "$CRAWLER_DIR" && runuser -u case-manager -- "$APP_DIR/.venv/bin/python" progress_storage.py init) \
    || echo "WARNING: couldn't create the case tracker's tables (does the database exist?)" >&2
fi

# The daily crawl of the case tracker's data (see case_progress_crawler/deploy/README.md):
# analysis.py needs poppler's pdftotext; the units run it from a systemd timer.
echo "--- Case tracker crawl (systemd timer)"
if [ -d "$CRAWLER_DIR/deploy" ]; then
  command -v pdftotext >/dev/null || apt-get install -y -q poppler-utils
  for u in case-progress-crawl.service case-progress-crawl.timer; do
    cmp -s "$CRAWLER_DIR/deploy/$u" /etc/systemd/system/$u || { cp "$CRAWLER_DIR/deploy/$u" /etc/systemd/system/$u; echo "updated $u"; }
  done
  systemctl daemon-reload
  systemctl enable --now case-progress-crawl.timer
fi

echo "--- Restarting $SERVICE"
systemctl restart "$SERVICE"
# /healthz answers even while the site is locked waiting for the encryption key.
for _ in $(seq 1 20); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/healthz || true)
  [ "$code" = 200 ] && { echo "OK: $SERVICE is answering (HTTP 200). It is LOCKED until an administrator enters the encryption key at /encryption-key."; exit 0; }
  sleep 1
done
echo "FAILED: $SERVICE isn't answering (last HTTP code: $code). Recent log:" >&2
journalctl -u "$SERVICE" -n 30 --no-pager >&2
exit 1
