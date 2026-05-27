#!/usr/bin/env bash
# host-sidecar/setup.sh — one-shot installer for the jarvis-sidecar daemon.
#
# - brew installs whisper-cpp + ffmpeg (no-op if already present)
# - downloads ggml-base.en.bin to ~/Library/Application Support/jarvis-sidecar/models/
# - generates a token at ~/Library/Application Support/jarvis-sidecar/token (mode 600)
# - creates a python venv under host-sidecar/.venv and installs the package
# - renders + loads ~/Library/LaunchAgents/com.jarvis.sidecar.plist (KeepAlive: true)

set -euo pipefail

STATE_DIR="$HOME/Library/Application Support/jarvis-sidecar"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
PLIST_DST="$LAUNCHD_DIR/com.jarvis.sidecar.plist"
PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/com.jarvis.sidecar.plist"
VENV="$(cd "$(dirname "$0")" && pwd)/.venv"
SIDECAR_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[1/6] brew install whisper-cpp ffmpeg"
brew install whisper-cpp ffmpeg 2>&1 | tail -n 3 || true

echo "[2/6] state dir: $STATE_DIR"
mkdir -p "$STATE_DIR/models"
chmod 700 "$STATE_DIR"

echo "[3/6] downloading ggml-base.en.bin (~150 MB)"
MODEL_PATH="$STATE_DIR/models/ggml-base.en.bin"
if [[ ! -f "$MODEL_PATH" ]]; then
  curl -fL \
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin" \
    -o "$MODEL_PATH"
else
  echo "    (model already present, skipping)"
fi

echo "[4/6] generating shared-secret token"
TOKEN_PATH="$STATE_DIR/token"
if [[ ! -f "$TOKEN_PATH" ]]; then
  python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "$TOKEN_PATH"
  chmod 600 "$TOKEN_PATH"
  echo "    (new token written; mode 600)"
else
  echo "    (existing token preserved)"
fi

echo "[5/6] installing python venv + jarvis_sidecar"
python3.11 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -e "$SIDECAR_DIR"

echo "[6/6] installing launchctl plist"
mkdir -p "$LAUNCHD_DIR"
# Render the plist with absolute paths inline (avoids env-var brittleness in launchd).
sed \
  -e "s|@@VENV@@|$VENV|g" \
  -e "s|@@HOME@@|$HOME|g" \
  "$PLIST_SRC" > "$PLIST_DST"
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

echo ""
echo "Done. Sidecar should be running on 127.0.0.1:9999."
echo "Token at: $TOKEN_PATH"
echo "Tail logs: tail -F \"$HOME/Library/Logs/jarvis-sidecar.log\""
