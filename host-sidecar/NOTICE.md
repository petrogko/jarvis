# jarvis-sidecar — Third-Party Notices

The sidecar shells out to external binaries and (optionally) external
Python packages. None of these are imported into JARVIS or the sidecar's
own code — each is invoked **only as a subprocess**, at a process
boundary. This arm's-length usage keeps JARVIS's own MIT license intact,
the same stance JARVIS takes toward macOS `say`, `whisper-cli`, and
`ffmpeg`.

## whisper.cpp (`whisper-cli`) — MIT

Local speech-to-text. Installed via Homebrew (`whisper-cpp`) by
`setup.sh`. Invoked as a subprocess by the `/stt` endpoint. MIT-licensed;
no copyleft obligation on JARVIS.

## ffmpeg

Audio transcoding (WebM/Opus → WAV for whisper, and the `say` AIFF →
AAC/M4A step). Installed via Homebrew. Invoked as a subprocess. ffmpeg
ships under LGPL/GPL components depending on build; JARVIS does not link
or bundle it — it is a separately-installed system tool called over a
process boundary only.

## Piper (OHF-Voice/piper1-gpl) — GPL-3.0

Neural local text-to-speech. **JARVIS never imports Piper.** It is
installed into an *isolated* Python venv under the sidecar state dir and
invoked **only as a subprocess** (`python -m piper`) by the `/tts`
endpoint. Because the only coupling is the argv/stdio process boundary —
identical in kind to how JARVIS treats `say`, `whisper-cli`, and
`ffmpeg` — Piper's GPL-3.0 does not reach JARVIS's MIT code.

Installation is **opt-in**: the default `./setup.sh` installs only
whisper + `say` (no GPL code on disk). The ~80MB GPL install happens
only when you run:

```bash
./setup.sh --with-piper
```

Voice models are SHA256-pinned at setup time (the pin slot warns if
unset). See `docs/superpowers/specs/2026-05-28-piper-tts-engine.md` for
the full design and trust model.
