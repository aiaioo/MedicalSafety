#!/usr/bin/env bash
# Builds the document editor's JS bundle(s) into static/dist/, so
# templates/document.html has something to load (see package.json's
# "build" script, which this wraps).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if [ ! -d node_modules ]; then
  npm install
fi

npm run build
