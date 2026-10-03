"use client";

import { useCallback, useEffect, useState } from "react";
import { api, unwrap } from "@/lib/api";
import { importanceText } from "@/lib/people";
import type { Page, PersonRow } from "@/lib/types";

export default function PeoplePage() {
  const [rows, setRows] = useState<PersonRow[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [sort, setSort] = useState<"importance" | "recent">("importance");
  const [q, setQ] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (after: string | null) => {
      try {
        const page = unwrap<Page<PersonRow>>(
          await api.GET("/api/v1/people", {
            params: { query: { sort, q: q.trim() || undefined, cursor: after ?? undefined } },
          }),
        );
        setRows((prev) => (after ? [...prev, ...page.items] : page.items));
        setCursor(page.next_cursor);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Could not load people");
      }
    },
    [sort, q],
  );

  useEffect(() => {
    void load(null);
  }, [load]);

  return (
    <>
      <h1>People</h1>
      <div className="row">
        <button onClick={() => setSort("importance")} disabled={sort === "importance"}>Most important</button>
        <button onClick={() => setSort("recent")} disabled={sort === "recent"}>Recent</button>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search by name or email" aria-label="Search" />
      </div>
      {error && <p className="error">{error}</p>}
      {rows.length === 0 ? (
        <p className="empty">No people yet.</p>
      ) : (
        <ul style={{ marginTop: 12 }}>
          {rows.map((p) => (
            <li key={p.id} className="card">
              <p className="title">
                <a href={`/people/${p.id}`}>{p.display_name ?? p.primary_email ?? "Unknown"}</a>
              </p>
              <p className="meta">
                {importanceText(p)}
                {p.relationship_type.value && <> · {p.relationship_type.value.replace("_", " ")}</>}
                {p.profile && (
                  <>
                    {" · "}
                    <span className="label">
                      You owe {p.profile.open_mine} · they owe {p.profile.open_theirs} (computed)
                    </span>
                  </>
                )}
              </p>
              {p.last_interaction_at && (
                <p className="meta">Last interaction {new Date(p.last_interaction_at).toLocaleDateString()}</p>
              )}
            </li>
          ))}
        </ul>
      )}
      {cursor && <button onClick={() => load(cursor)}>Load more</button>}
    </>
  );
}
