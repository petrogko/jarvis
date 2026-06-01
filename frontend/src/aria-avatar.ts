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
 *   - Formant-driven mouth shape: F1-band energy controls openness
 *     (jaw drop), F2/(F1+F2) ratio controls width (lip spread). Loosely
 *     tracks vowel shapes — "ah" pulls tall, "ee" pulls wide, "oo" stays
 *     small and rounded — without needing phoneme timestamps.
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
  // 512 bins covers ~12 kHz at 48 kHz sample rate / 2048 fftSize — enough
  // to bracket F1 (300–800 Hz) and F2 (800–2500 Hz) speech formants.
  const fftBuffer = new Uint8Array(512);

  // Mouth deformation is driven by two smoothed signals:
  //   openness — F1-band energy (mouth open vs closed; "ah" vs silence)
  //   width    — F2/(F1+F2) ratio (mouth wide vs round; "ee" vs "oo")
  let openness = 0;
  let width = 0.5;
  let pulse = 0;          // legacy amplitude — still used for filter boost
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
      // Contain-fit centered with a small inset margin. Cover-fit on a
      // fullscreen canvas produces an extreme close-up; contain-fit shows
      // the whole portrait and the dark background fills the edges.
      const iw = img.naturalWidth;
      const ih = img.naturalHeight;
      const INSET = 0.92;
      const scale = Math.min(w / iw, h / ih) * INSET;
      const drawW = iw * scale;
      const drawH = ih * scale;
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

      // Audio spectrum → formant-driven mouth shape.
      updateFormants();

      // Mouth-region overlay — non-uniform scale anchored at the mouth
      // center. openness lifts sy (mouth taller), width lifts sx (mouth
      // wider). The combination loosely tracks vowel shapes without
      // needing phoneme timestamps.
      if (state === "speaking" && pulse > 0.01) {
        drawMouthPulse(ctx!, img, dx, dy, drawW, drawH, openness, width, pulse);
      }

      // Blink — draw a thin black horizontal band over the eyes when the
      // blink animation is mid-flight. Eyes are roughly at cy ~ 0.41.
      blinkProgress = updateBlink(blinkProgress, now);
      if (blinkProgress < 1) {
        drawBlink(ctx!, dx, dy, drawW, drawH, blinkProgress);
      }

      // Vignette — radial fade from clear at the portrait center to the
      // background dark at the canvas edges. Hides the hard rectangle edge
      // of the contain-fit image so the avatar feels embedded in the dark
      // background rather than pasted on.
      drawVignette(ctx!, w, h, dx, dy, drawW, drawH);
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

  function updateFormants() {
    if (!analyser) {
      openness *= 0.9;
      width = width * 0.9 + 0.5 * 0.1;
      pulse *= 0.9;
      return;
    }
    analyser.getByteFrequencyData(fftBuffer);
    // Bin → frequency: i * sampleRate / fftSize. With 48 kHz / 2048,
    // binWidth ≈ 23.4 Hz. Bands:
    //   F1 ≈ 300–900 Hz  → bins 13..39
    //   F2 ≈ 900–2500 Hz → bins 39..107
    // (Slightly broadened from canonical 800Hz divider to capture both
    //  male and female formant ranges robustly.)
    let e1 = 0, e2 = 0, total = 0;
    for (let i = 13; i < 39; i++) e1 += fftBuffer[i];
    for (let i = 39; i < 107; i++) e2 += fftBuffer[i];
    for (let i = 8; i < 107; i++) total += fftBuffer[i];
    const e1Avg = e1 / (39 - 13) / 255;
    const e2Avg = e2 / (107 - 39) / 255;
    const totalAvg = total / (107 - 8) / 255;

    const opTarget = Math.min(1, e1Avg * 1.6);
    // F2/(F1+F2) — high when "ee/sh", low when "oo/aa-rounded".
    const ratio = (e1Avg + e2Avg) > 0.01
      ? e2Avg / (e1Avg + e2Avg)
      : 0.5;
    // Map [0.3, 0.7] → [0, 1] roughly; clamp.
    const wTarget = Math.max(0, Math.min(1, (ratio - 0.3) / 0.4));
    const pTarget = Math.min(1, totalAvg * 1.4);

    // Asymmetric smoothing: attack fast, release slow.
    openness = opTarget > openness ? openness + (opTarget - openness) * 0.5 : openness * 0.85;
    width = width + (wTarget - width) * 0.3;
    pulse = pTarget > pulse ? pulse + (pTarget - pulse) * 0.5 : pulse * 0.85;
  }

  function drawMouthPulse(
    c: CanvasRenderingContext2D,
    image: HTMLImageElement,
    dx: number, dy: number, drawW: number, drawH: number,
    op: number, wd: number, p: number,
  ) {
    const cx = dx + drawW * MOUTH_REGION.cx;
    const cy = dy + drawH * MOUTH_REGION.cy;
    const rx = drawW * MOUTH_REGION.rx;
    const ry = drawH * MOUTH_REGION.ry;

    c.save();
    // Wider clip so the asymmetric scale (especially sx) doesn't reveal a
    // seam at the cheek edges.
    c.beginPath();
    c.ellipse(cx, cy, rx * 1.35, ry * 1.6, 0, 0, Math.PI * 2);
    c.clip();

    // Non-uniform scale: openness drives sy (jaw drop), width drives sx
    // (lip spread). wd is centered at 0.5 — neutral mouth.
    const sx = 1 + op * 0.04 + (wd - 0.5) * 0.06;
    const sy = 1 + op * 0.12 - (wd - 0.5) * 0.03;
    c.translate(cx, cy);
    c.scale(sx, sy);
    c.translate(-cx, -cy);

    c.filter = `brightness(${1 + p * 0.10})`;
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

  function drawVignette(
    c: CanvasRenderingContext2D,
    w: number, h: number,
    dx: number, dy: number, drawW: number, drawH: number,
  ) {
    // Center of the portrait.
    const cx = dx + drawW / 2;
    const cy = dy + drawH * 0.45;  // weight slightly upward toward the face
    // Inner radius: enough to keep the face clear. Outer: reach the canvas
    // corners so the fade is complete at the edges.
    const inner = Math.max(drawW, drawH) * 0.42;
    const outer = Math.hypot(w, h) * 0.55;
    const grad = c.createRadialGradient(cx, cy, inner, cx, cy, outer);
    grad.addColorStop(0, "rgba(5, 5, 10, 0)");
    grad.addColorStop(0.7, "rgba(5, 5, 10, 0.55)");
    grad.addColorStop(1, "rgba(5, 5, 10, 0.95)");
    c.save();
    c.fillStyle = grad;
    c.fillRect(0, 0, w, h);
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
