/**
 * Aria Avatar — audio-reactive portrait alternative to the particle orb.
 *
 * Mirrors the Orb interface exactly so main.ts can use either as a
 * drop-in. State machine: idle | listening | thinking | speaking.
 *
 * Honest scope:
 * - This is NOT phoneme-accurate lip-sync. Real lip-sync from a still
 *   photo requires either a heavy on-device model (Wav2Lip / SadTalker /
 *   Hallo — all ~5GB+, GPU-bound, produce video after audio finishes,
 *   killing conversational pace) or a third-party API (D-ID / HeyGen —
 *   violates the local-only counsel posture).
 * - What this DOES do, in real time, in the browser:
 *   - Audio-amplitude-driven subtle mouth-region scale + opacity pulse.
 *   - Idle blink loop (every 4–7s).
 *   - State-driven brightness / saturation shifts (thinking dims slightly,
 *     listening warms slightly).
 * - Phase 2 follow-up: Piper exposes phoneme timestamps in its stdout —
 *   we can map those to viseme mouth poses for real lip-sync without
 *   leaving the host.
 */

export type AvatarState = "idle" | "listening" | "thinking" | "speaking";

export interface AriaAvatar {
  setState(s: AvatarState): void;
  setAnalyser(a: AnalyserNode | null): void;
}

const IMG_SRC = "/aria-avatar.png";

// Mouth region is roughly the lower-middle third of a portrait. These are
// fractions of the displayed image bounds, calibrated for the supplied
// portrait — adjust if a different image is dropped in.
const MOUTH_REGION = {
  cx: 0.495,  // center-x
  cy: 0.71,   // center-y (lower middle)
  rx: 0.13,   // half-width
  ry: 0.045,  // half-height
};

export function createAriaAvatar(canvas: HTMLCanvasElement): AriaAvatar {
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("aria-avatar: 2D canvas context unavailable");

  let state: AvatarState = "idle";
  let analyser: AnalyserNode | null = null;
  const fftBuffer = new Uint8Array(256);

  let pulse = 0;          // smoothed audio amplitude in [0, 1]
  let blinkProgress = 1;  // 0..1; 0 = eyes closed mid-blink, 1 = open
  let nextBlinkAt = performance.now() + 3000 + Math.random() * 3000;

  // Load image once.
  const img = new Image();
  let imgReady = false;
  img.onload = () => {
    imgReady = true;
  };
  img.onerror = () => {
    console.warn("[aria-avatar] image failed to load:", IMG_SRC);
  };
  img.src = IMG_SRC;

  // ---------- DPR + resize handling ----------------------------------------
  function resize() {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    ctx!.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  resize();
  window.addEventListener("resize", resize);

  // ---------- render loop --------------------------------------------------
  function render(now: number) {
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;

    // Background — same dark vibe the orb sits on so the toggle feels
    // continuous.
    ctx!.fillStyle = "#05050a";
    ctx!.fillRect(0, 0, w, h);

    if (imgReady) {
      // Cover-fit the image centered.
      const iw = img.naturalWidth;
      const ih = img.naturalHeight;
      const scaleCover = Math.max(w / iw, h / ih);
      const drawW = iw * scaleCover;
      const drawH = ih * scaleCover;
      const dx = (w - drawW) / 2;
      const dy = (h - drawH) / 2;

      // State-driven filter — listening warms, thinking dims, speaking
      // brightens slightly, idle is neutral.
      const filter = filterFor(state);
      ctx!.save();
      if (filter) {
        ctx!.filter = filter;
      }
      ctx!.drawImage(img, dx, dy, drawW, drawH);
      ctx!.restore();

      // Audio amplitude → pulse for the mouth region.
      pulse = updatePulse(pulse);

      // Mouth-region overlay — subtle scale + brightness shift centered on
      // the mouth. Done by re-drawing JUST the mouth rect with a scale
      // transform anchored at the mouth center.
      if (state === "speaking" && pulse > 0.01) {
        drawMouthPulse(ctx!, img, dx, dy, drawW, drawH, pulse);
      }

      // Blink — draw a thin black horizontal band over the eyes when the
      // blink animation is mid-flight. Eyes are roughly at cy ~ 0.41.
      blinkProgress = updateBlink(blinkProgress, now);
      if (blinkProgress < 1) {
        drawBlink(ctx!, dx, dy, drawW, drawH, blinkProgress);
      }
    }

    // State badge (small, low-contrast, bottom-right) — handy for debugging
    // and matches the orb's affordance of showing what mode she's in.
    drawStateBadge(ctx!, w, h, state);

    requestAnimationFrame(render);
  }
  requestAnimationFrame(render);

  // ---------- helpers ------------------------------------------------------

  function filterFor(s: AvatarState): string | null {
    switch (s) {
      case "thinking":  return "brightness(0.85) saturate(0.85)";
      case "listening": return "brightness(1.05) saturate(1.08)";
      case "speaking":  return "brightness(1.08)";
      case "idle":
      default:          return null;
    }
  }

  function updatePulse(prev: number): number {
    if (!analyser) {
      // Decay toward zero when no audio source.
      return prev * 0.9;
    }
    analyser.getByteFrequencyData(fftBuffer);
    // Use the low-mid band — speech energy lives there.
    let sum = 0;
    const lo = 8;
    const hi = 64;
    for (let i = lo; i < hi; i++) sum += fftBuffer[i];
    const avg = sum / (hi - lo) / 255;  // 0..1
    // Smooth: attack fast, release slow — feels alive.
    const target = Math.min(1, avg * 1.4);
    return target > prev ? prev + (target - prev) * 0.5 : prev * 0.85;
  }

  function drawMouthPulse(
    c: CanvasRenderingContext2D,
    image: HTMLImageElement,
    dx: number, dy: number, drawW: number, drawH: number,
    p: number,
  ) {
    const cx = dx + drawW * MOUTH_REGION.cx;
    const cy = dy + drawH * MOUTH_REGION.cy;
    const rx = drawW * MOUTH_REGION.rx;
    const ry = drawH * MOUTH_REGION.ry;

    c.save();
    // Clip to an ellipse around the mouth so the scale doesn't bulge the
    // chin or nose.
    c.beginPath();
    c.ellipse(cx, cy, rx * 1.15, ry * 1.4, 0, 0, Math.PI * 2);
    c.clip();

    // Scale around the mouth center proportional to amplitude.
    const s = 1 + p * 0.08;  // up to ~8% bigger
    c.translate(cx, cy);
    c.scale(s, s);
    c.translate(-cx, -cy);

    // Subtle brightness boost on the mouth area only.
    c.filter = `brightness(${1 + p * 0.12})`;

    c.drawImage(image, dx, dy, drawW, drawH);
    c.restore();
  }

  function updateBlink(prev: number, now: number): number {
    // The eyes-closed phase is short (~120ms); the opening eases.
    if (now >= nextBlinkAt) {
      // Start a blink.
      nextBlinkAt = now + 4000 + Math.random() * 3000;
      return 0;
    }
    if (prev < 1) {
      // Eyes are mid-blink; ease toward open.
      return Math.min(1, prev + 0.07);
    }
    return 1;
  }

  function drawBlink(
    c: CanvasRenderingContext2D,
    dx: number, dy: number, drawW: number, drawH: number,
    p: number,
  ) {
    // Eyes are at roughly cy = 0.41, cx = 0.50, half-width 0.18, half-height
    // 0.025. When p is small, the band is taller (more closed).
    const cx = dx + drawW * 0.50;
    const cy = dy + drawH * 0.41;
    const rx = drawW * 0.22;
    const ry = drawH * 0.027 * (1 - p);

    c.save();
    c.fillStyle = "rgba(0, 0, 0, 0.65)";
    c.beginPath();
    c.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2);
    c.fill();
    c.restore();
  }

  function drawStateBadge(
    c: CanvasRenderingContext2D,
    w: number, h: number,
    s: AvatarState,
  ) {
    const text = s === "idle" ? "" : s;
    if (!text) return;
    c.save();
    c.font = "10px ui-monospace, monospace";
    c.fillStyle = "rgba(56, 189, 248, 0.7)";
    c.textAlign = "right";
    c.textBaseline = "bottom";
    c.fillText(text + "…", w - 12, h - 10);
    c.restore();
  }

  // ---------- public API ---------------------------------------------------

  return {
    setState(s: AvatarState) {
      state = s;
    },
    setAnalyser(a: AnalyserNode | null) {
      analyser = a;
    },
  };
}
