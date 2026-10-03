"use client";

import { useEffect, useState } from "react";
import { api, unwrap } from "@/lib/api";
import type { Briefing, BriefingEntry } from "@/lib/types";

function localDate(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function Entries({ entries, empty }: { entries: BriefingEntry[]; empty: string }) {
  if (entries.length === 0) return <p className="empty">{empty}</p>;
  return (
    <ol>
      {entries.map((e) => (
        <li key={`${e.kind}:${e.id}`} className="card">
          <p className="title">
            {e.title}
            {e.person && <> — {e.person}</>}
          </p>
          <p className="meta">
            <span className={e.data_class === "ai_derived" ? "label ai" : "label"}>{e.label}</span>
            {e.due_local && <> · due {e.due_local}</>}
            {e.reasons.filter(Boolean).length > 0 && <> · {e.reasons.filter(Boolean).join(" · ")}</>}
            {e.source_item_ids.map((id, i) => (
              <a key={id} href={`/api/v1/sources/${id}`} target="_blank" rel="noreferrer">
                {" "}source {i + 1}
              </a>
            ))}
          </p>
        </li>
      ))}
    </ol>
  );
}

export default function BriefingPage() {
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        setBriefing(
          unwrap<Briefing>(await api.GET("/api/v1/briefings/{date}", { params: { path: { date: localDate() } } })),
        );
      } catch (e) {
        setError(e instanceof Error ? e.message : "Could not load the briefing");
      }
    })();
  }, []);

  if (error) return <p className="error">{error}</p>;
  if (!briefing) return <p className="empty">Loading…</p>;
  const s = briefing.sections;
  const c = s.communication;
  return (
    <>
      <h1>Good morning</h1>
      <p className="title">{briefing.headline}</p>
      <p className="meta">
        Prepared {new Date(briefing.generated_at).toLocaleTimeString()} from your items, mail and calendar (no AI
        writing).
      </p>
      {briefing.updated_since_briefing && (
        <p className="label warn">
          Updated since this briefing: {briefing.updated_since_briefing.material_changes} change(s). See Today.
        </p>
      )}

      <h2>Today&apos;s priorities</h2>
      <Entries entries={s.priorities} empty="Nothing urgent." />

      <h2>Important meetings</h2>
      {s.meetings.length === 0 ? (
        <p className="empty">No meetings today.</p>
      ) : (
        <ul>
          {s.meetings.map((m) => (
            <li key={m.id} className="card">
              <p className="title">
                {m.local_time} — {m.title}
              </p>
            </li>
          ))}
        </ul>
      )}

      <h2>Waiting on</h2>
      <Entries entries={s.waiting_on} empty="Nobody owes you anything open." />

      <h2>Deadlines</h2>
      <Entries entries={s.deadlines} empty="No deadlines in the next 3 days." />

      <h2>People requiring attention</h2>
      {s.people.length === 0 ? (
        <p className="empty">No one is waiting on you.</p>
      ) : (
        <ul>
          {s.people.map((p) => (
            <li key={p.id} className="card">
              <p className="title">
                <a href={`/people/${p.id}`}>{p.name ?? "Unknown"}</a>
              </p>
              <p className="meta">{p.reason}</p>
            </li>
          ))}
        </ul>
      )}

      <h2>Today&apos;s communication (last 24 hours)</h2>
      <p className="meta">
        {c.received} emails received · {c.require_attention} require attention · {c.with_action_items} contain
        potential action items · {c.with_deadlines} contain deadlines · {c.respond_today} require a response today
      </p>
    </>
  );
}
