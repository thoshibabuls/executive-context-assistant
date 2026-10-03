"use client";

// Meeting page and missed-meeting view (PRD §22, §25; BACKEND_DESIGN.md §16.9). Everything shown is a
// deterministic read of the meeting's extraction; AI-derived values carry their label and provenance.
import { useCallback, useEffect, useState } from "react";
import { ApiError, api, newIdempotencyKey, unwrap } from "@/lib/api";
import type { MeetingDecision, MeetingItem, MeetingPage, Speaker, TranscriptEvidence } from "@/lib/types";

function clock(ms: number | null): string {
  if (ms == null) return "";
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "";
}

function Quotes({ evidence }: { evidence: TranscriptEvidence[] }) {
  if (evidence.length === 0) return null;
  return (
    <ul className="citations">
      {evidence.map((e) => (
        <li key={e.id} className="meta">
          {e.start_ms != null && <span className="tag">{clock(e.start_ms)} </span>}
          {e.redacted ? <em>source deleted</em> : <>“{e.quote}”</>}
        </li>
      ))}
    </ul>
  );
}

function aiLabel(origin: string, status: string, band?: string | null): string {
  if (origin === "user") return "Added by you";
  if (status === "confirmed") return "AI-detected · confirmed by you";
  return `AI suggestion${band ? ` · ${band} confidence` : ""}`;
}

function ItemRow({ item }: { item: MeetingItem }) {
  return (
    <li className="card">
      <p className="title">{item.title}</p>
      <p className="meta">
        <span className="label ai">{aiLabel(item.origin, item.verification_status, item.provenance.confidence_band)}</span>
        {item.due_text && ` · due “${item.due_text}”`}
        {item.lifecycle_status !== "open" && ` · ${item.lifecycle_status}`}
      </p>
      <Quotes evidence={item.evidence} />
    </li>
  );
}

function DecisionRow({ d }: { d: MeetingDecision }) {
  return (
    <li className="card">
      <p className="title">{d.statement}</p>
      <p className="meta">
        <span className="label ai">{aiLabel(d.origin, d.verification_status, d.provenance.confidence_band)}</span>
        {d.resolved_by_id && " · resolved"}
        {d.superseded_by_id && " · superseded"}
      </p>
      <Quotes evidence={d.evidence} />
    </li>
  );
}

function Section({ title, items }: { title: string; items: MeetingItem[] }) {
  return (
    <>
      <h2>{title}</h2>
      {items.length === 0 ? <p className="empty">Nothing.</p> : <ul>{items.map((i) => <ItemRow key={i.id} item={i} />)}</ul>}
    </>
  );
}

function SpeakerRow({
  speaker,
  participants,
  onSave,
}: {
  speaker: Speaker;
  participants: MeetingPage["meeting"]["participants"];
  onSave: (label: string, personId: string | null) => Promise<void>;
}) {
  const [choice, setChoice] = useState<string>(speaker.person_id ?? "");
  const status =
    speaker.status === "applied"
      ? speaker.origin === "user"
        ? "confirmed by you"
        : `matched (${speaker.origin === "ai" ? "AI" : "rule"}${speaker.confidence != null ? `, ${Math.round(speaker.confidence * 100)}%` : ""})`
      : speaker.status === "proposed"
        ? `suggested${speaker.confidence != null ? ` (${Math.round(speaker.confidence * 100)}%)` : ""}: please confirm`
        : "unknown speaker: confirm who this is";
  return (
    <li className="card">
      <p className="title">
        {speaker.label}
        {speaker.person_name && ` → ${speaker.person_name}`}
      </p>
      <p className={`meta ${speaker.status !== "applied" ? "label warn" : ""}`}>{status}</p>
      <div className="row">
        <select value={choice} onChange={(e) => setChoice(e.target.value)} aria-label={`Who is ${speaker.label}`}>
          <option value="">None of these</option>
          {participants.map((p) => (
            <option key={p.person_id} value={p.person_id}>
              {p.name ?? "Unknown"}
              {p.is_self ? " (you)" : ""}
            </option>
          ))}
        </select>
        <button onClick={() => void onSave(speaker.label, choice || null)}>Confirm</button>
      </div>
    </li>
  );
}

export default function MeetingDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const [id, setId] = useState<string | null>(null);
  const [page, setPage] = useState<MeetingPage | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void params.then((p) => setId(p.id));
  }, [params]);

  const load = useCallback(async () => {
    if (!id) return;
    try {
      setPage(unwrap<MeetingPage>(await api.GET("/api/v1/meetings/{meeting_id}", { params: { path: { meeting_id: id } } })));
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) {
        window.location.href = "/api/v1/auth/google/login";
        return;
      }
      setError(e instanceof Error ? e.message : "Could not load the meeting");
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  async function saveSpeaker(label: string, personId: string | null) {
    if (!id || !page) return;
    try {
      await api.PUT("/api/v1/meetings/{meeting_id}/speakers", {
        params: { path: { meeting_id: id }, header: { "Idempotency-Key": newIdempotencyKey() } },
        body: { mappings: [{ label, person_id: personId }], base_version: page.meeting.version },
      });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save the speaker");
    }
  }

  if (error) return <p className="error">{error}</p>;
  if (!page) return <p className="empty">Loading…</p>;
  const m = page.meeting;
  const unmapped = page.speakers.filter((s) => s.status !== "applied");

  return (
    <>
      <h1>{m.title ?? "Untitled meeting"}</h1>
      <p className="meta">
        {when(m.starts_at)}
        {m.origin === "upload" && " · uploaded recording"}
        {m.status === "cancelled" && " · cancelled"}
      </p>
      <p className="row">
        <a href={`/meetings/${m.id}/prep`}>Prepare</a>
        <a href={`/chat?meeting=${m.id}`}>Ask about this meeting</a>
      </p>

      <h2>What happened</h2>
      {page.summary ? (
        <div className="card">
          <p className="label ai">
            {page.summary.label}
            {page.summary.provenance.model && ` · ${page.summary.provenance.model}`}
          </p>
          <p>{page.summary.text}</p>
          {page.summary.topics.length > 0 && <p className="meta">Topics: {page.summary.topics.join(", ")}</p>}
        </div>
      ) : (
        <p className="empty">
          {page.summary_status === "pending"
            ? "Summary pending: the recording is still being processed."
            : page.summary_status === "failed"
              ? "The summary could not be produced. The transcript stays searchable; you can retry from the Meetings page."
              : "No recording or transcript for this meeting."}
        </p>
      )}

      {page.summary && page.summary.concerns.length > 0 && (
        <>
          <h2>Concerns raised</h2>
          <ul>
            {page.summary.concerns.map((c) => (
              <li key={c.evidence_id} className="card">
                <span className="label ai">AI-detected </span>
                {c.start_ms != null && <span className="tag">{clock(c.start_ms)} </span>}
                {c.text}
              </li>
            ))}
          </ul>
        </>
      )}

      <h2>Decided</h2>
      {page.decisions.length === 0 ? <p className="empty">No decisions found.</p> : <ul>{page.decisions.map((d) => <DecisionRow key={d.id} d={d} />)}</ul>}

      <h2>Open questions</h2>
      {page.open_questions.length === 0 ? (
        <p className="empty">No open questions.</p>
      ) : (
        <ul>{page.open_questions.map((d) => <DecisionRow key={d.id} d={d} />)}</ul>
      )}

      <Section title="What you owe" items={page.items.yours} />
      <Section title="What others owe you" items={page.items.theirs} />
      <Section title="Other actions" items={page.items.others} />
      {page.items.unresolved.length > 0 && (
        <Section title="Needs a speaker confirmation (owner unclear)" items={page.items.unresolved} />
      )}

      <h2>What changed since the previous meeting</h2>
      {page.what_changed.previous_meeting ? (
        <>
          <p className="meta">
            Since <a href={`/meetings/${page.what_changed.previous_meeting.id}`}>{page.what_changed.previous_meeting.title ?? "the previous meeting"}</a>{" "}
            ({when(page.what_changed.previous_meeting.starts_at)})
          </p>
          {page.what_changed.changes.length === 0 ? (
            <p className="empty">No material changes.</p>
          ) : (
            <ul>
              {page.what_changed.changes.map((c) => (
                <li key={`${c.entity_type}-${c.entity_id}`} className="card">
                  <span className="tag">{c.kind} </span>
                  {c.title ?? c.entity_id}
                </li>
              ))}
            </ul>
          )}
        </>
      ) : (
        <p className="empty">{page.what_changed.note ?? "No previous related meeting."}</p>
      )}

      {page.speakers.length > 0 && (
        <>
          <h2>Speakers{unmapped.length > 0 && ` (${unmapped.length} to confirm)`}</h2>
          <ul>
            {page.speakers.map((s) => (
              <SpeakerRow key={s.label} speaker={s} participants={m.participants} onSave={saveSpeaker} />
            ))}
          </ul>
        </>
      )}
    </>
  );
}
