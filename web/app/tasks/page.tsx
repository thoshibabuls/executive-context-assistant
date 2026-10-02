"use client";

import { useCallback, useEffect, useState } from "react";
import { ItemCard } from "@/components/ItemCard";
import { api, newIdempotencyKey, unwrap } from "@/lib/api";
import type { Page, WorkItem } from "@/lib/types";

const DIRECTIONS = [
  ["", "All"],
  ["my_task", "My tasks"],
  ["my_commitment", "My commitments"],
  ["waiting_for", "Waiting for"],
  ["delegated", "Delegated"],
] as const;

type Direction = Exclude<(typeof DIRECTIONS)[number][0], "">;

export default function TasksPage() {
  const [direction, setDirection] = useState<Direction | "">("");
  const [items, setItems] = useState<WorkItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (after: string | null) => {
      try {
        const page = unwrap<Page<WorkItem>>(
          await api.GET("/api/v1/work-items", {
            params: { query: { direction: direction || undefined, sort: "due", cursor: after ?? undefined } },
          }),
        );
        setItems((prev) => (after ? [...prev, ...page.items] : page.items));
        setCursor(page.next_cursor);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Could not load tasks");
      }
    },
    [direction],
  );

  useEffect(() => {
    void load(null);
  }, [load]);

  async function create() {
    if (!title.trim()) return;
    try {
      const item = unwrap<WorkItem>(
        await api.POST("/api/v1/work-items", {
          params: { header: { "Idempotency-Key": newIdempotencyKey() } },
          body: { title: title.trim(), type: "task", direction: "my_task" },
        }),
      );
      setItems((prev) => [item, ...prev]);
      setTitle("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not create the task");
    }
  }

  return (
    <>
      <h1>Tasks</h1>
      <div className="row">
        {DIRECTIONS.map(([value, label]) => (
          <button key={value} onClick={() => setDirection(value)} disabled={direction === value}>
            {label}
          </button>
        ))}
      </div>
      <div className="row" style={{ margin: "12px 0" }}>
        <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Add a task" aria-label="New task" />
        <button onClick={create}>Add</button>
      </div>
      {error && <p className="error">{error}</p>}
      {items.length === 0 ? (
        <p className="empty">No open items.</p>
      ) : (
        <ul>
          {items.map((i) => (
            <ItemCard
              key={i.id}
              item={i}
              people={{}}
              onChange={(next) =>
                setItems((prev) => (next ? prev.map((x) => (x.id === i.id ? next : x)) : prev.filter((x) => x.id !== i.id)))
              }
            />
          ))}
        </ul>
      )}
      {cursor && <button onClick={() => load(cursor)}>Load more</button>}
    </>
  );
}
