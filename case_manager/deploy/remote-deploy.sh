#!/usr/bin/env bash
# The droplet's half of deploy.sh: deploy.sh copies the code (this file
# included) to $APP_DIR, then runs this there as root.
set -euo pipefail
export LC_ALL=C.UTF-8

APP_DIR=${APP_DIR:-/opt/case_manager}
ENV_FILE=/etc/case-manager/case-manager.env
BACKUP_DIR=/var/backups/case-manager
export ENCRYPTION_KEY_FILE=/etc/case-manager/encryption.key   # installed by deploy.sh
SERVICE=case-manager
# Migrations up to and including this one were applied by hand before
# schema_migrations existed; the first run records them as done.
BASELINE=26

cd "$APP_DIR"
chown -R -h case-manager:case-manager .

echo "--- Python dependencies"
sudo -u case-manager .venv/bin/pip install --quiet -r requirements.txt

[ -f "$ENCRYPTION_KEY_FILE" ] || { echo "Missing $ENCRYPTION_KEY_FILE" >&2; exit 1; }

# Keep the systemd unit in step with the repo (it isn't covered by rsync).
if ! cmp -s deploy/case-manager.service /etc/systemd/system/$SERVICE.service; then
  cp deploy/case-manager.service /etc/systemd/system/$SERVICE.service
  systemctl daemon-reload
  echo "updated the systemd unit"
fi

echo "--- Encrypting any plaintext files in storage/"
# Idempotent: already-encrypted files are skipped. Must finish before the
# restart, since the new code only reads .enc files.
sudo -u case-manager env ENCRYPTION_KEY_FILE="$ENCRYPTION_KEY_FILE" .venv/bin/python3 db/encrypt_existing_files.py

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

echo "--- Restarting $SERVICE"
systemctl restart "$SERVICE"
for _ in $(seq 1 20); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/signin || true)
  [ "$code" = 200 ] && { echo "OK: $SERVICE is answering (HTTP 200)"; exit 0; }
  sleep 1
done
echo "FAILED: $SERVICE isn't answering (last HTTP code: $code). Recent log:" >&2
journalctl -u "$SERVICE" -n 30 --no-pager >&2
exit 1
