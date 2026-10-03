"use client";

// Upload a meeting recording or transcript (PRD §21, §25; BACKEND_DESIGN.md §16.9). The dialog
// suggests overlapping or recent calendar meetings to link; an unlinked upload becomes its own
// meeting when processed.
import { useEffect, useState } from "react";
import { api, unwrap } from "@/lib/api";
import type { MeetingSuggestion, Recording } from "@/lib/types";
import { MEDIA_TYPES, TRANSCRIPT_TYPES, uploadRecording } from "@/lib/upload";

function local(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function UploadDialog({ onUploaded }: { onUploaded: (r: Recording) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [occurredAt, setOccurredAt] = useState("");
  const [meetingId, setMeetingId] = useState<string>("");
  const [suggestions, setSuggestions] = useState<MeetingSuggestion[]>([]);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const at = occurredAt ? new Date(occurredAt).toISOString() : undefined;
    api
      .GET("/api/v1/recordings/meeting-suggestions", { params: { query: { occurred_at: at } } })
      .then((r) => setSuggestions(unwrap<{ items: MeetingSuggestion[] }>(r).items))
      .catch(() => setSuggestions([]));
  }, [occurredAt]);

  async function submit() {
    if (!file || busy) return;
    setBusy(true);
    setError(null);
    try {
      const recording = await uploadRecording(file, {
        title: title.trim() || undefined,
        occurredAt: occurredAt ? new Date(occurredAt).toISOString() : undefined,
        meetingId: meetingId || null,
        onStatus: setStatus,
      });
      onUploaded(recording);
      setFile(null);
      setTitle("");
      setMeetingId("");
      setStatus("Uploaded. Processing continues in the background.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "The upload failed");
      setStatus(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card">
      <h2>Upload a meeting</h2>
      <p className="meta">
        Audio or video (up to 2 GB and 3 hours) or a transcript file (VTT, SRT, TXT, DOCX). Only the audio is
        processed; video frames are never sent to an AI model.
      </p>
      <div className="row">
        <input
          type="file"
          accept={[...MEDIA_TYPES, ...TRANSCRIPT_TYPES, ".vtt", ".srt", ".txt", ".docx"].join(",")}
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          aria-label="Recording or transcript file"
        />
      </div>
      <div className="row" style={{ marginTop: 8 }}>
        <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Meeting title (optional)" maxLength={200} />
        <input
          type="datetime-local"
          value={occurredAt}
          onChange={(e) => setOccurredAt(e.target.value)}
          aria-label="When the meeting started"
        />
      </div>
      <div className="row" style={{ marginTop: 8 }}>
        <label className="meta" htmlFor="link-meeting">
          Link to a calendar meeting:
        </label>
        <select id="link-meeting" value={meetingId} onChange={(e) => setMeetingId(e.target.value)}>
          <option value="">Not linked</option>
          {suggestions.map((s) => (
            <option key={s.id} value={s.id}>
              {s.title ?? "Untitled"} · {local(s.starts_at)}
              {s.reason === "overlaps" ? " (same time)" : ""}
            </option>
          ))}
        </select>
        <button onClick={() => void submit()} disabled={!file || busy}>
          Upload
        </button>
      </div>
      {status && <p className="meta">{status}</p>}
      {error && <p className="error">{error}</p>}
    </section>
  );
}
