import type { JarvisSocket } from "./ws";
import { pushUserLine } from "./transcript-panel";

/**
 * Text input for JARVIS — types route through the same WS transcript
 * channel the Web Speech API uses, so JARVIS reacts identically to
 * spoken or typed input.
 */
export function attachTextInput(socket: JarvisSocket): void {
  const container = document.getElementById("text-input");
  if (!container) {
    throw new Error("missing #text-input container");
  }

  container.innerHTML = `
    <form id="text-input-form" autocomplete="off">
      <input
        type="text"
        id="text-input-field"
        placeholder="Type a message…"
        autocomplete="off"
        spellcheck="false"
        aria-label="Type a message to JARVIS"
      />
      <button type="submit" id="text-input-send" aria-label="Send">↵</button>
    </form>
  `;

  const form = container.querySelector<HTMLFormElement>("#text-input-form")!;
  const field = container.querySelector<HTMLInputElement>("#text-input-field")!;

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const value = field.value.trim();
    if (!value) return;
    socket.send({ type: "transcript", text: value, isFinal: true });
    pushUserLine(value);
    field.value = "";
  });
}
