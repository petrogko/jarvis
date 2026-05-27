/**
 * Browser-side STT: record audio with MediaRecorder, POST the blob to
 * /api/stt, return the transcript.
 *
 * Used when vault key STT_PROVIDER === "whisper". Otherwise voice.ts falls
 * back to the existing Web Speech API path.
 */

import { withAuthHeaders } from "./auth-token";

export interface RecordingSession {
  stop(): Promise<string>;
  cancel(): void;
}

export async function startRecording(): Promise<RecordingSession> {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const recorder = new MediaRecorder(stream, { mimeType: "audio/webm" });
  const chunks: BlobPart[] = [];
  recorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) chunks.push(e.data);
  };
  recorder.start();

  let cancelled = false;

  return {
    async stop(): Promise<string> {
      return new Promise<string>((resolve, reject) => {
        recorder.onstop = async () => {
          stream.getTracks().forEach((t) => t.stop());
          if (cancelled) return resolve("");
          const blob = new Blob(chunks, { type: "audio/webm" });
          const form = new FormData();
          form.append("audio", blob, "clip.webm");
          try {
            const r = await fetch("/api/stt", withAuthHeaders({ method: "POST", body: form }));
            if (!r.ok) return reject(new Error(`HTTP ${r.status}`));
            const body = (await r.json()) as { text: string };
            resolve(body.text || "");
          } catch (err) {
            reject(err);
          }
        };
        recorder.stop();
      });
    },
    cancel() {
      cancelled = true;
      try { recorder.stop(); } catch {}
      stream.getTracks().forEach((t) => t.stop());
    },
  };
}
