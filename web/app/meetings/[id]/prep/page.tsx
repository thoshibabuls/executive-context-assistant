"use client";

// Meeting prep view (PRD §22; BACKEND_DESIGN.md §16.9). The sections are deterministic and shown at once.
// When they are not empty, the page requests AI suggested asks once per prep version; asks are labelled
// suggestions with the section lines they rest on, and the page works without them.
import { useCallback, useEffect, useState } from "react";
import { ApiError, api, unwrap } from "@/lib/api";

type PrepItem = {
  id?: string;
  entity_id?: string;
  title?: string | null;
  statement?: string;
  kind?: string;
  direction?: string;
  due_at?: string | null;
  due_text?: string | null;
  owner?: string | null;
  counterparty?: string | null;
  verification_status?: string;
  origin?: string;
};

type Prep = {
  meeting_id: string;
  key: string;
  version: number | null;
  empty: boolean;
  sections: {
    purpose: { title: string | null; description: string | null; starts_at: string; attendees: string[] };
    previous_meeting: { id: string; title: string | null; starts_at: string } | null;
    what_changed: PrepItem[];
    open_items_mine: PrepItem[];
    open_items_theirs: PrepItem[];
    unresolved_questions: PrepItem[];
    deadlines: PrepItem[];
  };
  asks: {
    status: "not_requested" | "pending" | "ready" | "failed" | "skipped";
    label: string;
    items: { text: string; citations: string[]; sources: { section: string; entity_id: string | null }[] }[];
    provenance: { model?: string | null } | null;
  };
};

function when(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "";
}

function Line({ item }: { item: PrepItem }) {
  const title = item.title ?? item.statement ?? "";
  const ai = item.origin !== "user" && item.verification_status === "suggested";
  return (
    <li className="card">
      <p className="title">{title}</p>
      <p className="meta">
        {item.kind && `${item.kind} · `}
        {item.owner && `owner ${item.owner} · `}
        {item.counterparty && `with ${item.counterparty} · `}
        {(item.due_text || item.due_at) && `due ${item.due_text ?? when(item.due_at)} · `}
        {ai ? <span className="label ai">AI suggestion</span> : <span className="label">confirmed</span>}
      </p>
    </li>
  );
}

function Block({ title, items, empty }: { title: string; items: PrepItem[]; empty: string }) {
  return (
    <>
      <h2>{title}</h2>
      {items.length === 0 ? <p className="empty">{empty}</p> : <ul>{items.map((i, n) => <Line key={i.id ?? i.entity_id ?? n} item={i} />)}</ul>}
    </>
  );
}

export default function PrepPage({ params }: { params: Promise<{ id: string }> }) {
  const [id, setId] = useState<string | null>(null);
  const [prep, setPrep] = useState<Prep | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void params.then((p) => setId(p.id));
  }, [params]);

  const load = useCallback(async () => {
    if (!id) return;
    try {
      const got = unwrap<Prep>(await api.GET("/api/v1/meetings/{meeting_id}/prep", { params: { path: { meeting_id: id } } }));
      setPrep(got);
      // Opening the view with non-empty sections requests asks once per version (AI-11).
      if (!got.empty && got.asks.status === "not_requested") {
        const requested = await api.POST("/api/v1/meetings/{meeting_id}/prep/asks", { params: { path: { meeting_id: id } } });
        if (requested.data) setPrep(requested.data as unknown as Prep);
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) {
        window.location.href = "/api/v1/auth/google/login";
        return;
      }
      setError(e instanceof Error ? e.message : "Could not load the prep view");
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  // Asks arrive in the background: poll while they are pending.
  useEffect(() => {
    if (!id || prep?.asks.status !== "pending") return;
    const timer = setInterval(async () => {
      try {
        setPrep(unwrap<Prep>(await api.GET("/api/v1/meetings/{meeting_id}/prep", { params: { path: { meeting_id: id } } })));
      } catch {
        /* keep the sections on a failed refresh */
      }
    }, 4000);
    return () => clearInterval(timer);
  }, [id, prep?.asks.status]);

  if (error) return <p className="error">{error}</p>;
  if (!prep) return <p className="empty">Loading…</p>;
  const s = prep.sections;

  return (
    <>
      <h1>Prepare: {s.purpose.title ?? "Untitled meeting"}</h1>
      <p className="meta">
        {when(s.purpose.starts_at)}
        {s.purpose.attendees.length > 0 && ` · with ${s.purpose.attendees.join(", ")}`}
      </p>
      {s.purpose.description && <p className="card">{s.purpose.description}</p>}
      <p className="row">
        <a href={`/meetings/${prep.meeting_id}`}>Meeting page</a>
        <a href={`/chat?meeting=${prep.meeting_id}`}>Ask about this meeting</a>
      </p>

      <h2>Suggested asks</h2>
      {prep.empty ? (
        <p className="empty">No open items or questions with these attendees.</p>
      ) : prep.asks.status === "ready" ? (
        <ul>
          {prep.asks.items.map((a, n) => (
            <li key={n} className="card">
              <span className="label ai">Suggestion </span>
              {a.text}
              <span className="meta"> (based on {a.citations.join(", ")})</span>
            </li>
          ))}
        </ul>
      ) : prep.asks.status === "pending" ? (
        <p className="meta">Preparing suggestions…</p>
      ) : prep.asks.status === "skipped" ? (
        <p className="meta">Suggestions are paused (daily AI budget); the sections below are complete.</p>
      ) : prep.asks.status === "failed" ? (
        <p className="meta">Suggestions are unavailable right now; the sections below are complete.</p>
      ) : null}

      <h2>What changed since the last meeting</h2>
      {s.previous_meeting ? (
        <>
          <p className="meta">
            Since <a href={`/meetings/${s.previous_meeting.id}`}>{s.previous_meeting.title ?? "the previous meeting"}</a> (
            {when(s.previous_meeting.starts_at)})
          </p>
          {s.what_changed.length === 0 ? <p className="empty">No material changes.</p> : <ul>{s.what_changed.map((c, n) => <Line key={n} item={c} />)}</ul>}
        </>
      ) : (
        <p className="empty">No previous related meeting.</p>
      )}

      <Block title="Unresolved questions" items={s.unresolved_questions} empty="No unresolved questions." />
      <Block title="Deadlines before the meeting" items={s.deadlines} empty="No deadlines before the meeting." />
      <Block title="What you owe them" items={s.open_items_mine} empty="Nothing open from you." />
      <Block title="What they owe you" items={s.open_items_theirs} empty="Nothing open from them." />
    </>
  );
}
