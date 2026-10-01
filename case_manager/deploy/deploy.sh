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
#      documents/ or the .venv, which live only on the droplet.
#   3. Runs deploy/remote-deploy.sh on the droplet: installs Python deps,
#      backs up the database and applies any new migrations, restarts the
#      service and checks that it answers.
set -euo pipefail

HOST=medicalsafety
APP_DIR=/opt/case_manager

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
trap 'rm -rf "$STAGE"' EXIT
git archive HEAD . | tar -x -C "$STAGE"
cp -R static/dist "$STAGE/static/dist"

echo "==> Copying $(git rev-parse --short HEAD) to $HOST:$APP_DIR"
# --no-o/--no-g: files arrive owned by root (we receive via sudo) and the
# remote script hands them to the service account. The excluded paths are
# the droplet's own state and are never deleted.
rsync -rlptz --delete --no-o --no-g \
  --exclude=/.venv --exclude=/storage --exclude=/documents --exclude=/.gunicorn --exclude=__pycache__ \
  --rsync-path="sudo rsync" -e ssh "$STAGE"/ "$HOST:$APP_DIR/"

echo "==> Running remote steps"
ssh "$HOST" "sudo env APP_DIR=$APP_DIR bash $APP_DIR/deploy/remote-deploy.sh"
