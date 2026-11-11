#!/usr/bin/env bash
# Deploys the committed, pushed code on main to the droplet.
#
#   deploy/deploy.sh
#
# Run from your laptop. It needs the `medicalsafety` host in ~/.ssh/config (the
# droplet, as a user with passwordless sudo -- see deploy/README.md).
#
# What it does:
#   1. Refuses unless the working tree is clean and HEAD is pushed, so what is
#      running on the droplet is always an identifiable commit.
#   2. Builds the editor's JS bundle here (the droplet has no node) and ships
#      exactly the committed files plus that bundle -- never storage/,
#      documents/ or the .venv, which live only on the droplet. The case
#      tracker's code (../case_progress_crawler, mounted by app.py at
#      /case_tracker/) goes to $CRAWLER_DIR beside it, again committed files
#      only: its data (pdfs/, case_details/, ...) is the droplet's own.
#   3. Runs deploy/remote-deploy.sh on the droplet: installs Python deps,
#      backs up the database and applies any new migrations, creates the case
#      tracker's tables, restarts the service and checks that it answers. The
#      encryption key is never part of a deploy: it lives only in the running
#      server's memory, so after every restart an administrator has to enter
#      it at /encryption-key.
set -euo pipefail

HOST=medicalsafety
APP_DIR=/opt/case_manager
CRAWLER_DIR=/opt/case_progress_crawler   # app.py looks for it beside APP_DIR

cd "$(dirname "${BASH_SOURCE[0]}")/.."        # the case_manager directory

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Uncommitted changes -- commit them first." >&2; exit 1
fi
git fetch --quiet origin main
if [ -n "$(git rev-list origin/main..HEAD)" ]; then
  echo "HEAD isn't pushed to origin/main -- push first." >&2; exit 1
fi

echo "==> Building the JS bundle"
[ -d node_modules ] || npm install
npm run build --silent

STAGE=$(mktemp -d)
git archive HEAD . | tar -x -C "$STAGE"
cp -R static/dist "$STAGE/static/dist"
CRAWLER_STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE" "$CRAWLER_STAGE"' EXIT
git -C "$(git rev-parse --show-toplevel)" archive HEAD:case_progress_crawler | tar -x -C "$CRAWLER_STAGE"

echo "==> Copying $(git rev-parse --short HEAD) to $HOST:$APP_DIR"
# --no-o/--no-g: files arrive owned by root (we receive via sudo) and the
# remote script hands them to the service account. The excluded paths are
# the droplet's own state and are never deleted.
rsync -rlptz --delete --no-o --no-g \
  --exclude=/.venv --exclude=/storage --exclude=/documents --exclude=/.gunicorn --exclude=__pycache__ \
  --rsync-path="sudo rsync" -e ssh "$STAGE"/ "$HOST:$APP_DIR/"

echo "==> Copying the case tracker's code to $HOST:$CRAWLER_DIR"
# Its data -- the PDFs and saved orders the viewer reads by path -- and its
# venv/logs are the droplet's own, like storage/ above.
rsync -rlptz --delete --no-o --no-g \
  --exclude=/.venv --exclude=/pdfs --exclude=/case_details --exclude=/manually_downloaded \
  --exclude=/causelists --exclude=/storage --exclude=/cron.log --exclude=__pycache__ \
  --rsync-path="sudo rsync" -e ssh "$CRAWLER_STAGE"/ "$HOST:$CRAWLER_DIR/"

echo "==> Running remote steps"
ssh "$HOST" "sudo env APP_DIR=$APP_DIR CRAWLER_DIR=$CRAWLER_DIR bash $APP_DIR/deploy/remote-deploy.sh"
