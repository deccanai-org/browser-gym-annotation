#!/bin/bash
# Runs the live-browser service that the annotator's gym pane connects to.
# Playwright resolves Chromium from PLAYWRIGHT_BROWSERS_PATH; without it the
# service inherits whatever sandbox cache its parent shell had and fails with
# "Executable doesn't exist".
set -euo pipefail

GYM_DIR="${GYM_DIR:-$HOME/Deccan AI/E Commerce Broswer Gym}"
PORT="${LIVE_BROWSER_PORT:-8877}"

export PLAYWRIGHT_BROWSERS_PATH="$HOME/Library/Caches/ms-playwright"

cd "$GYM_DIR"
exec .venv/bin/python -m uvicorn live_browser.service:app \
  --host 127.0.0.1 --port "$PORT" --log-level warning
