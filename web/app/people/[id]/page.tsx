"use client";

import { useCallback, useEffect, useState } from "react";
import { ContextCardRow } from "@/components/ContextCardRow";
import { ItemCard } from "@/components/ItemCard";
import { api, newIdempotencyKey, unwrap } from "@/lib/api";
import { importanceText } from "@/lib/people";
import type { ContextCard, PersonContext, WorkItem } from "@/lib/types";

const RELATIONSHIP_TYPES = [
  "executive",
  "client",
  "investor",
  "manager",
  "report",
  "partner",
  "stakeholder",
  "colleague",
  "vendor",
  "other",
  "low_priority",
] as const;

type RelationshipType = (typeof RELATIONSHIP_TYPES)[number];

const ORIGIN_LABEL: Record<string, string> = { user: "set by you", computed: "computed", inferred: "inferred" };

function Cards({ cards, empty }: { cards: ContextCard[]; empty: string }) {
  if (cards.length === 0) return <p className="empty">{empty}</p>;
  return (
    <ul>
      {cards.map((c, i) => (
        <ContextCardRow key={`${c.kind}:${c.entity_id ?? i}`} card={c} />
      ))}
    </ul>
  );
}

export default function PersonPage({ params }: { params: Promise<{ id: string }> }) {
  const [id, setId] = useState<string | null>(null);
  const [person, setPerson] = useState<PersonContext | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void params.then((p) => setId(p.id));
  }, [params]);

  const load = useCallback(async () => {
    if (!id) return;
    try {
      setPerson(
        unwrap<PersonContext>(await api.GET("/api/v1/people/{person_id}", { params: { path: { person_id: id } } })),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load this person");
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  async function correct(body: { importance_user?: number | null; relationship_type?: RelationshipType | null }) {
    if (!person) return;
    setBusy(true);
    setError(null);
    try {
      unwrap(
        await api.PATCH("/api/v1/people/{person_id}", {
          params: {
            path: { person_id: person.id },
            header: { "If-Match": `W/"${person.version}"`, "Idempotency-Key": newIdempotencyKey() },
          },
          body,
        }),
      );
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save the correction");
    } finally {
      setBusy(false);
    }
  }

  const updateItem = (list: keyof Pick<PersonContext, "they_owe_me" | "i_owe_them" | "other_items">) =>
    (itemId: string, next: WorkItem | null) =>
      person &&
      setPerson({
        ...person,
        [list]: next
          ? person[list].map((i) => (i.id === itemId ? next : i))
          : person[list].filter((i) => i.id !== itemId),
      });

  if (error && !person) return <p className="error">{error}</p>;
  if (!person) return <p className="empty">Loading…</p>;
  const profile = person.profile;
  const name = person.display_name ?? person.primary_email ?? "Unknown";

  return (
    <>
      <h1>{name}</h1>
      {person.redirected_from && <p className="meta">This person was merged; showing the combined person.</p>}
      <p className="meta">
        {person.primary_email}
        {person.organization && <> · {person.organization.name}</>}
        {person.role_title.value && (
          <>
            {" · "}
            {person.role_title.value} ({ORIGIN_LABEL[person.role_title.origin ?? ""] ?? "unknown"})
          </>
        )}
      </p>
      <p className="meta">{importanceText(person)}</p>
      <div className="row">
        <span className="tag">Importance:</span>
        {[1, 2, 3, 4, 5].map((n) => (
          <button
            key={n}
            onClick={() => correct({ importance_user: n })}
            disabled={busy || person.importance.user === n}
          >
            {n}
          </button>
        ))}
        {person.importance.user !== null && (
          <button onClick={() => correct({ importance_user: null })} disabled={busy}>
            Use computed
          </button>
        )}
      </div>
      <div className="row" style={{ marginTop: 8 }}>
        <span className="tag">Relationship:</span>
        <select
          value={person.relationship_type.value ?? ""}
          disabled={busy}
          onChange={(e) =>
            correct({ relationship_type: e.target.value ? (e.target.value as RelationshipType) : null })
          }
          aria-label="Relationship type"
        >
          <option value="">Not set</option>
          {RELATIONSHIP_TYPES.map((t) => (
            <option key={t} value={t}>
              {t.replace("_", " ")}
            </option>
          ))}
        </select>
      </div>
      {error && <p className="error">{error}</p>}

      {profile && (
        <>
          <h2>Relationship (computed)</h2>
          <p className="meta">
            Last 30 days: {profile.inbound_30d} message(s) from them, {profile.outbound_30d} to them,{" "}
            {profile.meetings_30d} meeting(s)
            {profile.interaction_recency_days !== null && <> · last two-way contact {profile.interaction_recency_days} day(s) ago</>}
          </p>
          {profile.active_topics.length > 0 && (
            <p className="meta">
              Topics:{" "}
              {profile.active_topics.map((t) => (
                <span key={t.text} className={t.origin === "inferred" ? "label ai" : "label"}>
                  {t.text} ({t.origin}){" "}
                </span>
              ))}
            </p>
          )}
          {profile.next_meeting_at && (
            <p className="meta">Next meeting {new Date(profile.next_meeting_at).toLocaleString()}</p>
          )}
        </>
      )}

      <h2>They owe you</h2>
      {person.they_owe_me.length === 0 ? (
        <p className="empty">Nothing open.</p>
      ) : (
        <ul>
          {person.they_owe_me.map((i) => (
            <ItemCard key={i.id} item={i} people={{}} onChange={(next) => updateItem("they_owe_me")(i.id, next)} />
          ))}
        </ul>
      )}

      <h2>You owe them</h2>
      {person.i_owe_them.length === 0 ? (
        <p className="empty">Nothing open.</p>
      ) : (
        <ul>
          {person.i_owe_them.map((i) => (
            <ItemCard key={i.id} item={i} people={{}} onChange={(next) => updateItem("i_owe_them")(i.id, next)} />
          ))}
        </ul>
      )}

      <h2>Recent threads</h2>
      <Cards cards={person.threads} empty="No threads in the last 30 days." />
      <h2>Meetings</h2>
      <Cards cards={person.meetings} empty="No recent or upcoming meetings." />
      <h2>Decisions</h2>
      <Cards cards={person.decisions} empty="No decisions in shared threads." />
      {person.notes.length > 0 && (
        <p className="meta">
          {person.notes.map((n) => (
            <span key={n}>{n} </span>
          ))}
        </p>
      )}
    </>
  );
}
