/**
 * Browser TTS voice preference.
 *
 * Backed by:
 *   - localStorage `jarvis.tts_voice` (fast-path; read by speakViaBrowser)
 *   - Vault key `TTS_VOICE` (canonical; hydrated into localStorage on boot
 *     via settings.ts loadPreferences())
 *
 * Empty / unset means "auto-detect" (the regex fallback in speakViaBrowser).
 */

const KEY = "jarvis.tts_voice";

export function getPreferredVoice(): string | null {
  try {
    const v = localStorage.getItem(KEY);
    return v && v.length > 0 ? v : null;
  } catch {
    return null;
  }
}

export function setPreferredVoice(name: string): void {
  try {
    if (name) localStorage.setItem(KEY, name);
    else localStorage.removeItem(KEY);
  } catch {
    // localStorage unavailable; speakViaBrowser falls back to auto-detect.
  }
}
