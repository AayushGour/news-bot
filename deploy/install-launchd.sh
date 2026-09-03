#!/usr/bin/env bash
# Install the pipeline as a launchd agent so it survives logout, reboot, and
# this terminal closing. Idempotent — safe to re-run after editing the plist.
set -euo pipefail

LABEL="com.aayush.newspipeline"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "==> checking prerequisites"
[ -f "$ROOT/.env" ]                     || { echo "missing .env";              exit 1; }
[ -f "$ROOT/secrets/telegram.session" ] || { echo "missing telegram.session";   exit 1; }
[ -x "$ROOT/.venv/bin/python" ]         || { echo "missing .venv";              exit 1; }

"$ROOT/.venv/bin/python" "$ROOT/scripts/preflight.py" || {
  echo; echo "preflight failed — fix the above before installing the service"; exit 1
}

echo "==> installing $PLIST"
mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data"
launchctl unload "$PLIST" 2>/dev/null || true
sed "s#/Users/aayushgour/Desktop/projects/telegram-automation#$ROOT#g" \
    "$ROOT/deploy/$LABEL.plist" > "$PLIST"
launchctl load "$PLIST"

echo
echo "installed and started."
echo "  logs:    tail -f $ROOT/data/pipeline.log"
echo "  status:  launchctl list | grep $LABEL"
echo "  stop:    launchctl unload $PLIST"
