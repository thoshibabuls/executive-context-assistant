"use client";

import { useCallback, useEffect, useState } from "react";
import { ItemCard } from "@/components/ItemCard";
import { ReminderRow } from "@/components/ReminderRow";
import { ApiError, api, unwrap } from "@/lib/api";
import type { Conversation, Person, Today, WorkItem } from "@/lib/types";

function ItemList({
  items,
  people,
  empty,
  onChange,
}: {
  items: WorkItem[];
  people: Record<string, Person>;
  empty: string;
  onChange: (id: string, next: WorkItem | null) => void;
}) {
  if (items.length === 0) return <p className="empty">{empty}</p>;
  return (
    <ul>
      {items.map((i) => (
        <ItemCard key={i.id} item={i} people={people} onChange={(next) => onChange(i.id, next)} />
      ))}
    </ul>
  );
}

function ConversationRow({ c }: { c: Conversation }) {
  return (
    <li className="card">
      <p className="title">{c.subject ?? "(no subject)"}</p>
      <p className="meta">
        {c.triage ? "AI triage suggests a reply" : "Unanswered message"}
        {c.priority.reasons.length > 0 && <> · {c.priority.reasons.map((r) => r.text).join(" · ")}</>}
      </p>
      {c.latest_snippet && <p className="meta">{c.latest_snippet}</p>}
    </li>
  );
}

export default function TodayPage() {
  const [today, setToday] = useState<Today | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setToday(unwrap<Today>(await api.GET("/api/v1/today")));
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) {
        window.location.href = "/api/v1/auth/google/login";
        return;
      }
      setError(e instanceof Error ? e.message : "Could not load Today");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const replace = (list: WorkItem[], id: string, next: WorkItem | null) =>
    next ? list.map((i) => (i.id === id ? next : i)) : list.filter((i) => i.id !== id);

  const onChange = (id: string, next: WorkItem | null) => {
    if (!today) return;
    setToday({
      ...today,
      commitments: replace(today.commitments, id, next),
      waiting_for: replace(today.waiting_for, id, next),
      deadlines: replace(today.deadlines, id, next),
      new_suggestions: replace(today.new_suggestions, id, next),
      attention: today.attention.flatMap((a) =>
        a.kind === "work_item" && a.item.id === id ? (next ? [{ kind: "work_item" as const, item: next }] : []) : [a],
      ),
    });
  };

  if (error) return <p className="error">{error}</p>;
  if (!today) return <p className="empty">Loading…</p>;
  const people = today.people;

  return (
    <>
      <h1>Today · {today.date}</h1>

      <h2>{today.attention.length} things need your attention</h2>
      {today.attention.length === 0 ? (
        <p className="empty">Nothing urgent right now.</p>
      ) : (
        <ul>
          {today.attention.map((a) =>
            a.kind === "work_item" ? (
              <ItemCard key={a.item.id} item={a.item} people={people} onChange={(n) => onChange(a.item.id, n)} />
            ) : (
              <ConversationRow key={a.conversation.id} c={a.conversation} />
            ),
          )}
        </ul>
      )}

      {today.reminders.length > 0 && (
        <>
          <h2>Reminders</h2>
          <ul>
            {today.reminders.map((r) => (
              <ReminderRow
                key={r.id}
                reminder={r}
                onChange={(next) =>
                  setToday({
                    ...today,
                    reminders:
                      next.state === "delivered" || next.state === "snoozed"
                        ? today.reminders.map((x) => (x.id === next.id ? next : x))
                        : today.reminders.filter((x) => x.id !== next.id),
                  })
                }
              />
            ))}
          </ul>
        </>
      )}

      <h2>Meetings</h2>
      {today.meetings.length === 0 ? (
        <p className="empty">No meetings today.</p>
      ) : (
        <ul>
          {today.meetings.map((m) => (
            <li key={m.id} className="card">
              <p className="title">{m.title ?? "(untitled)"}</p>
              <p className="meta">
                {new Date(m.starts_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} –{" "}
                {new Date(m.ends_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                {m.status === "cancelled" && " · cancelled"}
              </p>
            </li>
          ))}
        </ul>
      )}

      <h2>Your commitments</h2>
      <ItemList items={today.commitments} people={people} empty="No open commitments." onChange={onChange} />

      <h2>Waiting for</h2>
      <ItemList items={today.waiting_for} people={people} empty="Nobody owes you anything open." onChange={onChange} />

      <h2>Deadlines (next 7 days)</h2>
      <ItemList items={today.deadlines} people={people} empty="No deadlines this week." onChange={onChange} />

      <h2>Needs a response</h2>
      {today.needs_response.length === 0 ? (
        <p className="empty">You are caught up.</p>
      ) : (
        <ul>
          {today.needs_response.map((c) => (
            <ConversationRow key={c.id} c={c} />
          ))}
        </ul>
      )}

      <h2>New suggestions</h2>
      <ItemList items={today.new_suggestions} people={people} empty="No new suggestions." onChange={onChange} />
    </>
  );
}
