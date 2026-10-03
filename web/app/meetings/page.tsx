"use client";

import { useCallback, useEffect, useState } from "react";
import { UploadDialog } from "@/components/UploadDialog";
import { ApiError, api, unwrap } from "@/lib/api";
import type { Meeting, Page, Recording } from "@/lib/types";

// Status by stage (BACKEND_DESIGN.md §16.9): what the pipeline is doing with each upload.
const STATUS_TEXT: Record<Recording["status"], string> = {
  pending_upload: "Waiting for the upload to finish",
  uploaded: "Uploaded, waiting to be processed",
  preparing: "Extracting audio",
  transcribing: "Transcribing",
  transcribed: "Transcribed, extracting decisions and items",
  extracting: "Extracting decisions and items",
  ready: "Processed",
  failed: "Failed",
  rejected: "Not accepted",
};

const ERROR_TEXT: Record<string, string> = {
  duration_limit: "Recordings longer than 3 hours are not accepted.",
  weekly_limit: "This would exceed 10 hours of meetings this week.",
  checksum_mismatch: "The uploaded file differs from the file that was checked; upload it again.",
  no_audio: "The file has no audio track.",
  unreadable: "The file could not be read.",
};

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "";
}

function RecordingRow({ recording, onChange }: { recording: Recording; onChange: (r: Recording) => void }) {
  const [error, setError] = useState<string | null>(null);

  async function retry() {
    try {
      onChange(
        unwrap<Recording>(
          await api.POST("/api/v1/recordings/{recording_id}/retry", {
            params: { path: { recording_id: recording.id } },
          }),
        ),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "Retry failed");
    }
  }

  return (
    <li className="card">
      <p className="title">{recording.title ?? (recording.kind === "media" ? "Recording" : "Transcript file")}</p>
      <p className="meta">
        {STATUS_TEXT[recording.status]}
        {recording.failed_stage && ` at ${recording.failed_stage}`}
        {recording.error_code && ` · ${ERROR_TEXT[recording.error_code] ?? recording.error_code}`}
        {recording.duration_s != null && ` · ${Math.round(recording.duration_s / 60)} min`}
        {recording.created_at && ` · uploaded ${when(recording.created_at)}`}
      </p>
      <div className="row">
        {recording.meeting_id && <a href={`/meetings/${recording.meeting_id}`}>Open meeting</a>}
        {recording.status === "failed" && <button onClick={() => void retry()}>Retry</button>}
      </div>
      {error && <p className="error">{error}</p>}
    </li>
  );
}

export default function MeetingsPage() {
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const recs = unwrap<Page<Recording>>(await api.GET("/api/v1/recordings", { params: { query: { limit: 20 } } }));
      setRecordings(recs.items);
      const from = new Date(Date.now() - 14 * 86400_000).toISOString();
      const to = new Date(Date.now() + 7 * 86400_000).toISOString();
      const list = unwrap<Page<Meeting>>(await api.GET("/api/v1/meetings", { params: { query: { from, to } } }));
      setMeetings(list.items);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        window.location.href = "/api/v1/auth/google/login";
        return;
      }
      setError(e instanceof Error ? e.message : "Could not load meetings");
    }
  }, []);

  useEffect(() => {
    void load();
    // Processing runs in the background: refresh the status list while something is in progress.
    const timer = setInterval(() => void load(), 15_000);
    return () => clearInterval(timer);
  }, [load]);

  return (
    <>
      <h1>Meetings</h1>
      <UploadDialog onUploaded={(r) => setRecordings((prev) => [r, ...prev.filter((x) => x.id !== r.id)])} />
      {error && <p className="error">{error}</p>}
      <h2>Uploads</h2>
      {recordings.length === 0 ? (
        <p className="empty">No uploads yet.</p>
      ) : (
        <ul>
          {recordings.map((r) => (
            <RecordingRow
              key={r.id}
              recording={r}
              onChange={(next) => setRecordings((prev) => prev.map((x) => (x.id === next.id ? next : x)))}
            />
          ))}
        </ul>
      )}
      <h2>Calendar (last 14 days and next 7)</h2>
      {meetings.length === 0 ? (
        <p className="empty">No meetings in this period.</p>
      ) : (
        <ul>
          {meetings.map((m) => (
            <li key={m.id} className="card">
              <p className="title">
                <a href={`/meetings/${m.id}`}>{m.title ?? "Untitled meeting"}</a>
              </p>
              <p className="meta">
                {when(m.starts_at)}
                {m.status === "cancelled" && " · cancelled"}
                {m.status !== "cancelled" && new Date(m.starts_at).getTime() > Date.now() && (
                  <>
                    {" · "}
                    <a href={`/meetings/${m.id}/prep`}>Prepare</a>
                  </>
                )}
              </p>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
