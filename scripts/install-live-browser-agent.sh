#!/bin/bash
# Installs live-browser as a launchd user agent so it survives shell/terminal
# teardown and restarts automatically if it crashes.
#
#   ./scripts/install-live-browser-agent.sh            # install + start
#   launchctl bootout gui/$UID/ai.deccan.live-browser  # stop + uninstall
set -euo pipefail

LABEL="ai.deccan.live-browser"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GYM_DIR="${GYM_DIR:-$HOME/Deccan AI/E Commerce Broswer Gym}"
LOG="$HOME/Library/Logs/${LABEL}.log"
TARGET="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [ ! -x "$GYM_DIR/.venv/bin/python" ]; then
  echo "error: no venv python at $GYM_DIR/.venv/bin/python" >&2
  echo "set GYM_DIR to the gym checkout and retry" >&2
  exit 1
fi

chmod +x "$HERE/live-browser.sh"
mkdir -p "$HOME/Library/LaunchAgents" "$(dirname "$LOG")"

sed -e "s|__SCRIPT__|$HERE/live-browser.sh|g" \
    -e "s|__GYM_DIR__|$GYM_DIR|g" \
    -e "s|__LOG__|$LOG|g" \
    "$HERE/${LABEL}.plist" > "$TARGET"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$TARGET"
launchctl enable "gui/$UID/$LABEL"

for _ in $(seq 1 20); do
  if curl -sf -m2 http://127.0.0.1:8877/live/health >/dev/null; then
    echo "live-browser healthy: $(curl -s http://127.0.0.1:8877/live/health)"
    echo "logs: $LOG"
    exit 0
  fi
  sleep 1
done

echo "error: live-browser did not become healthy in 20s; see $LOG" >&2
exit 1
