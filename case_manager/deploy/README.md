# Deploying to a DigitalOcean droplet

The only secret this app has is `DATABASE_URL` (see `storage.py`'s module
docstring — it's the sole thing in the codebase that reads it). On a single
droplet, a root-only env file loaded by systemd is the right-sized way to
keep that out of git and out of the app's own directory: no extra service to
run, no third-party dependency, and rotating the password is "edit the file,
restart the service."

## One-time server setup

```bash
# 1. PostgreSQL + a dedicated app role (not the postgres superuser)
sudo apt install postgresql
sudo -u postgres createuser case_manager_app --pwprompt   # set a strong password when prompted
sudo -u postgres createdb case_manager --owner case_manager_app

# 2. A dedicated, unprivileged system account to run the app as
sudo useradd --system --home /opt/case_manager --shell /usr/sbin/nologin case-manager

# 3. Get the code
sudo git clone <this repo's URL> /opt/case_manager
cd /opt/case_manager/case_manager   # or wherever this directory ends up

# 4. Python deps, including gunicorn (the production WSGI server -- the
#    Flask dev server app.py falls back to via `python3 app.py` is for local
#    development only and must never be what's actually serving requests)
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt gunicorn

# 5. The document editor's JS bundle
sudo npm install && sudo npm run build

sudo chown -R case-manager:case-manager /opt/case_manager

# 6. Load the schema
psql "postgresql://case_manager_app:<password>@localhost/case_manager" -f db/schema.sql

# 7. The secrets file systemd will inject as this process's environment --
#    see case-manager.env.example for the exact commands and template.
sudo install -d -m 700 -o case-manager -g case-manager /etc/case-manager
sudo install -m 600 -o case-manager -g case-manager /dev/null /etc/case-manager/case-manager.env
sudoedit /etc/case-manager/case-manager.env   # paste in the real DATABASE_URL

# 8. The service
sudo cp deploy/case-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now case-manager
sudo systemctl status case-manager   # should be "active (running)"
```

Put a reverse proxy (nginx or Caddy) in front for TLS; this service only
binds `127.0.0.1:8000` and is not meant to face the internet directly. That
proxy setup isn't included here since it's independent of the app itself.

## Bringing existing data along

If you're moving from a machine that was still using the old
`storage/*.json` files (pre-database), copy that `storage/` and `documents/`
directory to the droplet, then run the one-off importer once against the
now-schema'd database:

```bash
sudo -u case-manager DATABASE_URL="postgresql://case_manager_app:<password>@localhost/case_manager" \
    .venv/bin/python3 db/migrate_json_to_postgres.py
```

## Rotating the database password

```bash
sudo -u postgres psql -c "ALTER ROLE case_manager_app WITH PASSWORD '<new password>';"
sudoedit /etc/case-manager/case-manager.env   # update DATABASE_URL to match
sudo systemctl restart case-manager
```

## Updating code

```bash
cd /opt/case_manager && sudo -u case-manager git pull
sudo -u case-manager .venv/bin/pip install -r requirements.txt gunicorn
sudo systemctl restart case-manager
```
