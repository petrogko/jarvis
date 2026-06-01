/**
 * Voice input (Web Speech API) and audio output (AudioContext) for JARVIS.
 */

// ---------------------------------------------------------------------------
// Speech Recognition
// ---------------------------------------------------------------------------

export interface VoiceInput {
  start(): void;
  stop(): void;
  pause(): void;
  resume(): void;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const webkitSpeechRecognition: any;

export function createVoiceInput(
  onTranscript: (text: string) => void,
  onError: (msg: string) => void
): VoiceInput {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const SR = (window as any).SpeechRecognition || (typeof webkitSpeechRecognition !== "undefined" ? webkitSpeechRecognition : null);
  if (!SR) {
    onError("Speech recognition not supported in this browser");
    return { start() {}, stop() {}, pause() {}, resume() {} };
  }

  const recognition = new SR();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = "en-US";

  let shouldListen = false;
  let paused = false;

  recognition.onresult = (event: any) => {
    for (let i = event.resultIndex; i < event.results.length; i++) {
      if (event.results[i].isFinal) {
        const text = event.results[i][0].transcript.trim();
        if (text) onTranscript(text);
      }
    }
  };

  recognition.onend = () => {
    if (shouldListen && !paused) {
      try {
        recognition.start();
      } catch {
        // Already started
      }
    }
  };

  recognition.onerror = (event: any) => {
    if (event.error === "not-allowed") {
      onError("Microphone access denied. Please allow microphone access.");
      shouldListen = false;
    } else if (event.error === "no-speech") {
      // Normal, just restart
    } else if (event.error === "aborted") {
      // Expected during pause
    } else {
      console.warn("[voice] recognition error:", event.error);
    }
  };

  return {
    start() {
      shouldListen = true;
      paused = false;
      try {
        recognition.start();
      } catch {
        // Already started
      }
    },
    stop() {
      shouldListen = false;
      paused = false;
      recognition.stop();
    },
    pause() {
      paused = true;
      recognition.stop();
    },
    resume() {
      paused = false;
      if (shouldListen) {
        try {
          recognition.start();
        } catch {
          // Already started
        }
      }
    },
  };
}

// ---------------------------------------------------------------------------
// Browser TTS Fallback
// ---------------------------------------------------------------------------

/**
 * Speak text via window.speechSynthesis (browser-native TTS).
 *
 * Used as a safety net when the backend cannot produce audio bytes
 * (e.g. FISH_API_KEY missing, Docker container without macOS `say`).
 * Synthesis happens entirely in the browser — no network, no server.
 */
import { getPreferredVoice } from "./voice-pref";

export function speakViaBrowser(text: string): void {
  if (!("speechSynthesis" in window)) {
    console.warn("[tts-fallback] speechSynthesis not available");
    return;
  }
  // Cancel any in-flight utterance so we don't queue up backlogs.
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  // Voice selection: explicit user preference > regex over common natural
  // voices > browser default. getVoices() may return [] before
  // `voiceschanged` fires; falling through to browser default is acceptable.
  const voices = window.speechSynthesis.getVoices();
  const preferredName = getPreferredVoice();
  const selected =
    (preferredName ? voices.find((v) => v.name === preferredName) : undefined) ||
    voices.find((v) => /alex|samantha|daniel|fiona|karen|moira|tessa|tom/i.test(v.name)) ||
    null;
  if (selected) utterance.voice = selected;
  utterance.rate = 1.0;
  utterance.pitch = 1.0;
  window.speechSynthesis.speak(utterance);
}

// ---------------------------------------------------------------------------
// Audio Player
// ---------------------------------------------------------------------------

export interface AudioPlayer {
  enqueue(base64: string): Promise<void>;
  stop(): void;
  getAnalyser(): AnalyserNode;
  setRegister(name: string): void;
  onFinished(cb: () => void): void;
}

export function createAudioPlayer(): AudioPlayer {
  const audioCtx = new AudioContext();

  // Voice chain — sits between the buffer source and the analyser so the
  // lip-sync formant readout reflects what the user actually hears.
  //
  //   source → lowShelf → presence → highShelf → compressor → outGain
  //          → analyser → destination
  //
  // Filter values shift per register (driven by the [REG:X] marker the
  // persona emits): soft is closer and warmer, counsel is intimate and
  // quieter, dry is more present and less compressed, playful is brighter.
  // Smooth ramps (setTargetAtTime) between presets so transitions don't
  // click or thump.
  const lowShelf = audioCtx.createBiquadFilter();
  lowShelf.type = "lowshelf";
  lowShelf.frequency.value = 200;
  lowShelf.gain.value = 0;

  const presence = audioCtx.createBiquadFilter();
  presence.type = "peaking";
  presence.frequency.value = 2400;
  presence.Q.value = 0.9;
  presence.gain.value = 0;

  const highShelf = audioCtx.createBiquadFilter();
  highShelf.type = "highshelf";
  highShelf.frequency.value = 6500;
  highShelf.gain.value = -3;

  const compressor = audioCtx.createDynamicsCompressor();
  compressor.threshold.value = -22;
  compressor.ratio.value = 2.5;
  compressor.knee.value = 6;
  compressor.attack.value = 0.005;
  compressor.release.value = 0.12;

  const outGain = audioCtx.createGain();
  outGain.gain.value = 1.0;

  const analyser = audioCtx.createAnalyser();
  analyser.fftSize = 2048;
  analyser.smoothingTimeConstant = 0.8;

  lowShelf.connect(presence);
  presence.connect(highShelf);
  highShelf.connect(compressor);
  compressor.connect(outGain);
  outGain.connect(analyser);
  analyser.connect(audioCtx.destination);

  // Register presets. Each tweak is small — we're tinting her, not
  // remixing. Conservative on purpose; bigger deltas start sounding like
  // a different person rather than the same person in a different mood.
  type RegisterPreset = {
    lowShelfGain: number;
    presenceGain: number;
    highShelfGain: number;
    compThreshold: number;
    compRatio: number;
    outGain: number;
  };
  const REGISTER_PRESETS: Record<string, RegisterPreset> = {
    neutral: { lowShelfGain: 0,   presenceGain: 0,   highShelfGain: -3, compThreshold: -22, compRatio: 2.5, outGain: 1.00 },
    soft:    { lowShelfGain: 1.5, presenceGain: 0,   highShelfGain: -5, compThreshold: -26, compRatio: 3.5, outGain: 0.95 },
    counsel: { lowShelfGain: 1.0, presenceGain: -1,  highShelfGain: -4, compThreshold: -28, compRatio: 4.0, outGain: 0.92 },
    dry:     { lowShelfGain: -1,  presenceGain: 1.5, highShelfGain: -1, compThreshold: -18, compRatio: 2.0, outGain: 1.02 },
    playful: { lowShelfGain: 0,   presenceGain: 1.5, highShelfGain:  0, compThreshold: -20, compRatio: 2.0, outGain: 1.05 },
  };

  function applyRegister(name: string) {
    const p = REGISTER_PRESETS[name] || REGISTER_PRESETS.neutral;
    const t = audioCtx.currentTime;
    const tc = 0.08;  // smooth 80ms ramp — fast enough to land on phrase start, slow enough to not click
    lowShelf.gain.setTargetAtTime(p.lowShelfGain, t, tc);
    presence.gain.setTargetAtTime(p.presenceGain, t, tc);
    highShelf.gain.setTargetAtTime(p.highShelfGain, t, tc);
    compressor.threshold.setTargetAtTime(p.compThreshold, t, tc);
    compressor.ratio.setTargetAtTime(p.compRatio, t, tc);
    outGain.gain.setTargetAtTime(p.outGain, t, tc);
  }

  const queue: AudioBuffer[] = [];
  let isPlaying = false;
  let currentSource: AudioBufferSourceNode | null = null;
  let finishedCallback: (() => void) | null = null;

  function playNext() {
    if (queue.length === 0) {
      isPlaying = false;
      currentSource = null;
      finishedCallback?.();
      return;
    }

    isPlaying = true;
    const buffer = queue.shift()!;
    const source = audioCtx.createBufferSource();
    source.buffer = buffer;
    source.connect(lowShelf);
    currentSource = source;

    source.onended = () => {
      if (currentSource === source) {
        playNext();
      }
    };

    source.start();
  }

  return {
    async enqueue(base64: string) {
      // Resume audio context (browser autoplay policy)
      if (audioCtx.state === "suspended") {
        await audioCtx.resume();
      }

      try {
        const binary = atob(base64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) {
          bytes[i] = binary.charCodeAt(i);
        }
        const audioBuffer = await audioCtx.decodeAudioData(bytes.buffer.slice(0));
        queue.push(audioBuffer);
        if (!isPlaying) playNext();
      } catch (err) {
        console.error("[audio] decode error:", err);
        // Skip bad audio, continue
        if (!isPlaying && queue.length > 0) playNext();
      }
    },

    stop() {
      queue.length = 0;
      if (currentSource) {
        try {
          currentSource.stop();
        } catch {
          // Already stopped
        }
        currentSource = null;
      }
      isPlaying = false;
    },

    getAnalyser() {
      return analyser;
    },

    setRegister(name: string) {
      applyRegister(name);
    },

    onFinished(cb: () => void) {
      finishedCallback = cb;
    },
  };
}
