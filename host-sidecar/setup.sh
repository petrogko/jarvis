#!/usr/bin/env bash
# host-sidecar/setup.sh — one-shot installer for the jarvis-sidecar daemon.
#
# - brew installs whisper-cpp + ffmpeg (no-op if already present)
# - downloads ggml-base.en.bin to ~/Library/Application Support/jarvis-sidecar/models/
# - generates a token at ~/Library/Application Support/jarvis-sidecar/token (mode 600)
# - creates a python venv under host-sidecar/.venv and installs the package
# - renders + loads ~/Library/LaunchAgents/com.jarvis.sidecar.plist (KeepAlive: true)
#
# Optional: pass --with-piper to also install Piper neural TTS (OHF-Voice/
# piper1-gpl, GPL-3.0). Piper is installed in its OWN isolated venv and only
# ever invoked as a subprocess — JARVIS never imports it, keeping JARVIS's MIT
# license clear. ~80 MB. Default setup installs whisper + say only.

set -euo pipefail

WITH_PIPER=0
for arg in "$@"; do
  case "$arg" in
    --with-piper) WITH_PIPER=1 ;;
  esac
done

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

if [[ "$WITH_PIPER" == "1" ]]; then
  echo "[piper] installing Piper neural TTS (GPL-3.0, isolated venv)"
  PIPER_VENV="$STATE_DIR/piper-venv"
  PIPER_DATA="$STATE_DIR/piper-voices"
  mkdir -p "$PIPER_DATA"

  python3.11 -m venv "$PIPER_VENV"
  "$PIPER_VENV/bin/pip" install --quiet --upgrade pip
  "$PIPER_VENV/bin/pip" install --quiet piper-tts

  echo "[piper] downloading voice en_GB-alan-medium"
  MODEL="$PIPER_DATA/en_GB-alan-medium.onnx"
  if [[ ! -f "$MODEL" ]]; then
    "$PIPER_VENV/bin/python" -m piper.download_voices en_GB-alan-medium --data-dir "$PIPER_DATA"
  else
    echo "    (voice already present, skipping)"
  fi

  # SHA256 integrity pin (security-advisor recommendation). Fill PIN_ONNX with
  # the hash from a first trusted download: shasum -a 256 "$MODEL"
  # Empty PIN_ONNX = skip verification + warn.
  PIN_ONNX=""
  if [[ -n "$PIN_ONNX" ]]; then
    echo "$PIN_ONNX  $MODEL" | shasum -a 256 -c - \
      || { echo "[piper] MODEL CHECKSUM MISMATCH — aborting"; exit 1; }
  else
    echo "[piper] WARNING: no SHA256 pin set for the voice model; integrity unverified."
  fi

  echo "[piper] done — set TTS_ENGINE=piper in JARVIS settings to use it."
fi

echo ""
echo "Done. Sidecar should be running on 127.0.0.1:9999."
echo "Token at: $TOKEN_PATH"
echo "Tail logs: tail -F \"$HOME/Library/Logs/jarvis-sidecar.log\""
