# jarvis-sidecar

macOS host daemon for JARVIS. Exposes local TTS (`say`) and STT (`whisper-cli`)
to the JARVIS Docker container over loopback HTTP.

See `docs/superpowers/specs/2026-05-26-jarvis-sidecar-design.md` for the design
and trust model.

## Quick start

```bash
./setup.sh    # T9 — installs brew deps + downloads model + writes token + loads launchctl plist
```

## Uninstall

```bash
./teardown.sh # T9 — unloads launchctl plist + removes token + state dir
```

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | /health | X-SIDECAR-Token | service status |
| POST | /tts | X-SIDECAR-Token | text → AAC/M4A audio bytes |
| POST | /stt | X-SIDECAR-Token | audio multipart → transcript text |

Default bind: `127.0.0.1:9999`. Never exposed to LAN.
