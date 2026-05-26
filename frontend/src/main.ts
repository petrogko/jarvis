/**
 * JARVIS — Main entry point.
 *
 * Wires together the orb visualization, WebSocket communication,
 * speech recognition, and audio playback into a single experience.
 */

import { createOrb, type OrbState } from "./orb";
import { createVoiceInput, createAudioPlayer, speakViaBrowser } from "./voice";
import { createSocket } from "./ws";
import { openSettings, checkFirstTimeSetup } from "./settings";
import { awaitUnlock } from "./lock-screen";
import { withAuthHeaders } from "./auth-token";
import { attachTranscript, toggleTranscript, pushUserLine } from "./transcript-panel";
import { attachTextInput } from "./text-input";
import "./style.css";

(async () => {
  await awaitUnlock();

  // ---------------------------------------------------------------------------
  // State machine
  // ---------------------------------------------------------------------------

  type State = "idle" | "listening" | "thinking" | "speaking";
  let currentState: State = "idle";
  // Mic defaults to OFF — user explicitly unmutes when they want voice input.
  // Typing still works via the text input regardless of mute state.
  let isMuted = true;

  const statusEl = document.getElementById("status-text")!;
  const errorEl = document.getElementById("error-text")!;

  function showError(msg: string) {
    errorEl.textContent = msg;
    errorEl.style.opacity = "1";
    setTimeout(() => {
      errorEl.style.opacity = "0";
    }, 5000);
  }

  function updateStatus(state: State) {
    const labels: Record<State, string> = {
      idle: "",
      listening: "listening...",
      thinking: "thinking...",
      speaking: "",
    };
    statusEl.textContent = labels[state];
  }

  // ---------------------------------------------------------------------------
  // Init components
  // ---------------------------------------------------------------------------

  const canvas = document.getElementById("orb-canvas") as HTMLCanvasElement;
  const orb = createOrb(canvas);

  const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const WS_URL = `${wsProto}//${window.location.host}/ws/voice`;
  const socket = createSocket(WS_URL);

  const audioPlayer = createAudioPlayer();
  orb.setAnalyser(audioPlayer.getAnalyser());

  // Attach debug transcript panel — must happen before onMessage wiring below
  attachTranscript(socket);
  attachTextInput(socket);

  function transition(newState: State) {
    if (newState === currentState) return;
    currentState = newState;
    orb.setState(newState as OrbState);
    updateStatus(newState);

    // Show the stop button only while JARVIS is producing audio.
    const stopBtn = document.getElementById("btn-stop");
    if (stopBtn) {
      stopBtn.style.display = newState === "speaking" ? "inline-flex" : "none";
    }

    switch (newState) {
      case "idle":
        if (!isMuted) voiceInput.resume();
        break;
      case "listening":
        if (!isMuted) voiceInput.resume();
        break;
      case "thinking":
        voiceInput.pause();
        break;
      case "speaking":
        voiceInput.pause();
        break;
    }
  }

  // ---------------------------------------------------------------------------
  // Voice input
  // ---------------------------------------------------------------------------

  const voiceInput = createVoiceInput(
    (text: string) => {
      // Cancel any current JARVIS response before sending new input
      audioPlayer.stop();
      // User spoke — send transcript and echo to conversation panel
      socket.send({ type: "transcript", text, isFinal: true });
      pushUserLine(text);
      transition("thinking");
    },
    (msg: string) => {
      showError(msg);
    }
  );

  // ---------------------------------------------------------------------------
  // Audio playback finished
  // ---------------------------------------------------------------------------

  audioPlayer.onFinished(() => {
    transition("idle");
  });

  // ---------------------------------------------------------------------------
  // WebSocket messages
  // ---------------------------------------------------------------------------

  socket.onMessage((msg) => {
    const type = msg.type as string;

    if (type === "audio") {
      const audioData = msg.data as string | undefined;
      const text = msg.text as string | undefined;
      console.log("[audio] received", audioData ? `${audioData.length} chars` : "EMPTY", "state:", currentState);
      if (audioData && audioData.length > 0) {
        // Normal path: backend produced audio bytes — decode and play.
        if (currentState !== "speaking") {
          transition("speaking");
        }
        audioPlayer.enqueue(audioData);
      } else if (text) {
        // Fallback: backend has no audio (no Fish key, Docker container, etc.).
        // Speak via the browser so the user still hears JARVIS.
        console.warn("[tts-fallback] no audio bytes — falling back to speechSynthesis");
        transition("speaking");
        speakViaBrowser(text);
        // speechSynthesis has no AudioContext integration; transition to idle
        // after the utterance ends (or immediately if synthesis unavailable).
        const synth = window.speechSynthesis;
        if (synth) {
          // Poll until the utterance finishes then return to idle.
          const poll = setInterval(() => {
            if (!synth.speaking) {
              clearInterval(poll);
              transition("idle");
            }
          }, 250);
        } else {
          transition("idle");
        }
      } else {
        // Neither audio bytes nor text — nothing to do.
        console.warn("[audio] no data or text received, returning to idle");
        transition("idle");
      }
      // Log text for debugging
      if (text) console.log("[JARVIS]", text);
    } else if (type === "status") {
      const state = msg.state as string;
      if (state === "thinking" && currentState !== "thinking") {
        transition("thinking");
      } else if (state === "working") {
        // Task spawned — show thinking with a different label
        transition("thinking");
        statusEl.textContent = "working...";
      } else if (state === "idle") {
        transition("idle");
      }
    } else if (type === "text") {
      // Text fallback when TTS fails
      console.log("[JARVIS]", msg.text);
    } else if (type === "task_spawned") {
      console.log("[task]", "spawned:", msg.task_id, msg.prompt);
    } else if (type === "task_complete") {
      console.log("[task]", "complete:", msg.task_id, msg.status, msg.summary);
    }
  });

  // ---------------------------------------------------------------------------
  // Kick off
  // ---------------------------------------------------------------------------

  // Mic is muted by default — start the voice-input engine but pause it
  // immediately so it's ready when the user unmutes. The orb shows the
  // "idle" state; the user can either type into the text input or click
  // the mic button to start listening.
  setTimeout(() => {
    voiceInput.start();
    voiceInput.pause();
    btnMute.classList.add("muted");
    transition("idle");
  }, 1000);

  // Resume AudioContext on ANY user interaction (browser autoplay policy)
  function ensureAudioContext() {
    const ctx = audioPlayer.getAnalyser().context as AudioContext;
    if (ctx.state === "suspended") {
      ctx.resume().then(() => console.log("[audio] context resumed"));
    }
  }
  document.addEventListener("click", ensureAudioContext);
  document.addEventListener("touchstart", ensureAudioContext);
  document.addEventListener("keydown", ensureAudioContext, { once: true });

  // Try to resume audio context on load
  ensureAudioContext();

  // ---------------------------------------------------------------------------
  // UI Controls
  // ---------------------------------------------------------------------------

  const btnMute = document.getElementById("btn-mute")!;
  const btnStop = document.getElementById("btn-stop")!;
  const btnMenu = document.getElementById("btn-menu")!;
  const menuDropdown = document.getElementById("menu-dropdown")!;
  const btnRestart = document.getElementById("btn-restart")!;
  const btnFixSelf = document.getElementById("btn-fix-self")!;
  const btnTranscriptToggle = document.getElementById("btn-transcript-toggle")!;

  btnStop.addEventListener("click", (e) => {
    e.stopPropagation();
    // Stop both audio paths: streamed bytes via AudioContext, and browser
    // speechSynthesis fallback. Whichever was playing, it ends here.
    audioPlayer.stop();
    if (window.speechSynthesis) {
      window.speechSynthesis.cancel();
    }
    transition("idle");
  });

  btnMute.addEventListener("click", (e) => {
    e.stopPropagation();
    isMuted = !isMuted;
    btnMute.classList.toggle("muted", isMuted);
    if (isMuted) {
      voiceInput.pause();
      transition("idle");
    } else {
      voiceInput.resume();
      transition("listening");
    }
  });

  btnMenu.addEventListener("click", (e) => {
    e.stopPropagation();
    menuDropdown.style.display = menuDropdown.style.display === "none" ? "block" : "none";
  });

  document.addEventListener("click", () => {
    menuDropdown.style.display = "none";
  });

  btnRestart.addEventListener("click", async (e) => {
    e.stopPropagation();
    menuDropdown.style.display = "none";
    statusEl.textContent = "restarting...";
    try {
      await fetch("/api/restart", withAuthHeaders({ method: "POST" }));
      // Wait a few seconds then reload
      setTimeout(() => window.location.reload(), 4000);
    } catch {
      statusEl.textContent = "restart failed";
    }
  });

  btnFixSelf.addEventListener("click", (e) => {
    e.stopPropagation();
    menuDropdown.style.display = "none";
    // Activate work mode on the WebSocket session (JARVIS becomes Claude Code's voice)
    socket.send({ type: "fix_self" });
    statusEl.textContent = "entering work mode...";
  });

  // Transcript toggle button
  btnTranscriptToggle.addEventListener("click", (e) => {
    e.stopPropagation();
    menuDropdown.style.display = "none";
    const isVisible = toggleTranscript();
    btnTranscriptToggle.textContent = isVisible ? "Hide transcript" : "Show transcript";
  });

  // Settings button
  const btnSettings = document.getElementById("btn-settings")!;
  btnSettings.addEventListener("click", (e) => {
    e.stopPropagation();
    menuDropdown.style.display = "none";
    openSettings();
  });

  // First-time setup detection — check after a short delay for server readiness
  setTimeout(() => {
    checkFirstTimeSetup();
  }, 2000);
})();
