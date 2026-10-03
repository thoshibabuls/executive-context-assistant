// Meeting upload flow (BACKEND_DESIGN.md §11.4, §16.9): hash → init (Idempotency-Key) → PUT to the
// pre-signed URL → complete. Known media (same SHA-256) returns the existing recording without upload.
import { api, newIdempotencyKey, unwrap } from "./api";
import { sha256File } from "./sha256";
import type { Recording, UploadInitResponse } from "./types";

export const MEDIA_TYPES = [
  "audio/mpeg",
  "audio/mp4",
  "audio/x-m4a",
  "audio/wav",
  "audio/x-wav",
  "audio/webm",
  "audio/ogg",
  "audio/flac",
  "audio/aac",
  "video/mp4",
  "video/webm",
  "video/quicktime",
  "video/x-matroska",
];
export const TRANSCRIPT_TYPES = [
  "text/vtt",
  "application/x-subrip",
  "text/plain",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
];

const BY_EXTENSION: Record<string, string> = {
  vtt: "text/vtt",
  srt: "application/x-subrip",
  txt: "text/plain",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  m4a: "audio/x-m4a",
  mkv: "video/x-matroska",
  mov: "video/quicktime",
};

/** The MIME type to declare: the browser's, else one inferred from the extension. */
export function mimeOf(file: File): string {
  const ext = file.name.split(".").pop()?.toLowerCase() ?? "";
  const known = [...MEDIA_TYPES, ...TRANSCRIPT_TYPES];
  if (file.type && known.includes(file.type)) return file.type;
  return BY_EXTENSION[ext] ?? file.type;
}

export type UploadOptions = {
  title?: string;
  occurredAt?: string;
  meetingId?: string | null;
  onStatus?: (text: string) => void;
};

export async function uploadRecording(file: File, options: UploadOptions = {}): Promise<Recording> {
  const say = options.onStatus ?? (() => undefined);
  const mime = mimeOf(file);
  say("Computing checksum…");
  const sha256 = await sha256File(file, (f) => say(`Computing checksum… ${Math.round(f * 100)}%`));
  const init = unwrap<UploadInitResponse>(
    await api.POST("/api/v1/recordings", {
      body: {
        mime,
        bytes: file.size,
        sha256,
        title: options.title || null,
        occurred_at: options.occurredAt || null,
        meeting_id: options.meetingId ?? null,
      },
      params: { header: { "Idempotency-Key": newIdempotencyKey() } },
    }),
  );
  if (init.duplicate || !init.upload) {
    say("This file was uploaded before; showing the existing recording.");
    return init.recording;
  }
  say("Uploading…");
  const put = await fetch(init.upload.url, {
    method: init.upload.method,
    headers: init.upload.headers,
    body: file,
    credentials: "same-origin",
  });
  if (!put.ok) throw new Error(`Upload failed (HTTP ${put.status})`);
  say("Starting processing…");
  return unwrap<Recording>(
    await api.POST("/api/v1/recordings/{recording_id}/complete", {
      params: { path: { recording_id: init.recording.id } },
    }),
  );
}
