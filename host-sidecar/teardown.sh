#!/usr/bin/env bash
# host-sidecar/teardown.sh — uninstall.
#
# - unloads launchctl plist
# - removes ~/Library/LaunchAgents/com.jarvis.sidecar.plist
# - removes ~/Library/Application Support/jarvis-sidecar/ (token + models)
#
# Leaves the host-sidecar/.venv in place (cheap to recreate; user may want
# to re-install).

set -euo pipefail

STATE_DIR="$HOME/Library/Application Support/jarvis-sidecar"
PLIST_DST="$HOME/Library/LaunchAgents/com.jarvis.sidecar.plist"

if [[ -f "$PLIST_DST" ]]; then
  launchctl unload "$PLIST_DST" 2>/dev/null || true
  rm -f "$PLIST_DST"
  echo "[1/2] plist unloaded and removed"
fi

if [[ -d "$STATE_DIR" ]]; then
  rm -rf "$STATE_DIR"
  echo "[2/2] state dir removed: $STATE_DIR"
fi

echo "Done. Sidecar uninstalled."
