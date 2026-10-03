"use client";

import { useState } from "react";
import { api, newIdempotencyKey, unwrap } from "@/lib/api";
import type { Person, WorkItem } from "@/lib/types";

const DIRECTION_LABEL: Record<string, string> = {
  my_task: "Your task",
  my_commitment: "You committed",
  delegated: "Delegated",
  waiting_for: "Waiting for",
  shared: "Shared",
  observed: "Observed",
  unresolved: "Owner unclear",
};

function provenanceLabel(item: WorkItem): string {
  if (item.origin === "user") return "Added by you";
  if (item.verification_status === "confirmed") return "AI-detected · confirmed by you";
  const band = item.provenance.confidence_band ? ` · ${item.provenance.confidence_band} confidence` : "";
  return `AI suggestion${band}`;
}

function due(item: WorkItem): string | null {
  if (!item.due_at) return item.due_text;
  const d = new Date(item.due_at);
  const text = item.due_precision === "datetime" ? d.toLocaleString() : d.toLocaleDateString();
  return item.due_text ? `${text} (“${item.due_text}”)` : text;
}

export function ItemCard({
  item,
  people,
  onChange,
}: {
  item: WorkItem;
  people: Record<string, Person>;
  onChange: (next: WorkItem | null) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(item.title);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const counterparty = item.counterparty_person_id ? people[item.counterparty_person_id] : undefined;

  async function run(fn: () => Promise<WorkItem | null>) {
    setBusy(true);
    setError(null);
    try {
      onChange(await fn());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  const runCommand = (name: "confirm" | "reject" | "complete") =>
    run(async () => {
      const result = await api.POST("/api/v1/work-items/{item_id}/{command}", {
        params: { path: { item_id: item.id, command: name }, header: { "Idempotency-Key": newIdempotencyKey() } },
      });
      const next = unwrap<WorkItem>(result);
      return name === "reject" || name === "complete" ? null : next;
    });

  // Your priority wins over the computed score and teaches the per-user weights (slice 3.5).
  const setPriority = (override: 1 | -1 | 0) =>
    run(async () =>
      unwrap<WorkItem>(
        await api.PATCH("/api/v1/work-items/{item_id}", {
          params: { path: { item_id: item.id }, header: { "If-Match": `W/"${item.version}"` } },
          body: { priority_override: override },
        }),
      ),
    );

  const save = () =>
    run(async () => {
      const result = await api.PATCH("/api/v1/work-items/{item_id}", {
        params: { path: { item_id: item.id }, header: { "If-Match": `W/"${item.version}"` } },
        body: { title },
      });
      setEditing(false);
      return unwrap<WorkItem>(result);
    });

  return (
    <li className="card">
      <div className="row">
        <span className="tag">{DIRECTION_LABEL[item.direction] ?? item.direction}</span>
        <span className={item.origin === "ai" && item.verification_status === "suggested" ? "label ai" : "label"}>
          {provenanceLabel(item)}
        </span>
        {item.has_conflict && <span className="label warn">Conflicting information</span>}
        {item.has_source_gap && <span className="label warn">Source deleted</span>}
      </div>
      {editing ? (
        <div className="row">
          <input value={title} onChange={(e) => setTitle(e.target.value)} aria-label="Title" />
          <button onClick={save} disabled={busy}>Save</button>
          <button onClick={() => setEditing(false)} disabled={busy}>Cancel</button>
        </div>
      ) : (
        <p className="title">{item.title}</p>
      )}
      <p className="meta">
        {counterparty && <>With {counterparty.display_name ?? counterparty.primary_email} · </>}
        {due(item) && <>Due {due(item)} · </>}
        {item.priority.reasons.map((r) => r.text).join(" · ")}
      </p>
      {item.provenance.evidence_source_ids.length > 0 && (
        <p className="meta">
          Evidence:{" "}
          {item.provenance.evidence_source_ids.map((id, i) => (
            <a key={id} href={`/api/v1/sources/${id}`} target="_blank" rel="noreferrer">
              source {i + 1}
            </a>
          ))}
        </p>
      )}
      <div className="row">
        {item.verification_status === "suggested" && (
          <>
            <button onClick={() => runCommand("confirm")} disabled={busy}>Confirm</button>
            <button onClick={() => runCommand("reject")} disabled={busy}>Not a task</button>
          </>
        )}
        {!editing && <button onClick={() => setEditing(true)} disabled={busy}>Edit</button>}
        <button onClick={() => runCommand("complete")} disabled={busy}>Done</button>
        <button onClick={() => setPriority(1)} disabled={busy} title="Pin this item high">More important</button>
        <button onClick={() => setPriority(-1)} disabled={busy} title="Pin this item low">Less important</button>
      </div>
      {error && <p className="error">{error}</p>}
    </li>
  );
}
