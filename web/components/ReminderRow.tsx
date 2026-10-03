"use client";

import { useState } from "react";
import { api, unwrap } from "@/lib/api";
import type { Reminder } from "@/lib/types";

function itemLabel(r: Reminder): string {
  const p = r.item_provenance;
  if (!p) return "";
  if (p.origin === "user") return "Your item";
  if (p.verification_status === "confirmed") return "AI-detected · confirmed by you";
  return `AI suggestion${p.confidence_band ? ` · ${p.confidence_band} confidence` : ""}`;
}

/** A reminder with snooze, dismiss and done (conditional transitions on the server). */
export function ReminderRow({ reminder, onChange }: { reminder: Reminder; onChange: (next: Reminder) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(fn: () => Promise<Reminder>) {
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

  const snooze = (preset: "1h" | "3h" | "tomorrow") =>
    run(async () =>
      unwrap<Reminder>(
        await api.POST("/api/v1/reminders/{reminder_id}/snooze", {
          params: { path: { reminder_id: reminder.id } },
          body: { preset },
        }),
      ),
    );
  const command = (name: "dismiss" | "acted") =>
    run(async () =>
      unwrap<Reminder>(
        await api.POST("/api/v1/reminders/{reminder_id}/{command}", {
          params: { path: { reminder_id: reminder.id, command: name } },
        }),
      ),
    );

  const open = reminder.state === "pending" || reminder.state === "delivered" || reminder.state === "snoozed";
  return (
    <li className="card">
      <p className="title">{reminder.text}</p>
      <p className="meta">
        <span className="label">Reminder ({reminder.reminder_type.replace("_", " ")})</span>
        {itemLabel(reminder) && <span className="label"> · {itemLabel(reminder)}</span>}
        {reminder.state === "snoozed" && reminder.snoozed_until && (
          <> · snoozed until {new Date(reminder.snoozed_until).toLocaleString()}</>
        )}
        {reminder.proactive === false && reminder.state === "delivered" && <> · shown here only (daily limit)</>}
        {reminder.item_provenance?.evidence_source_ids.map((id, i) => (
          <a key={id} href={`/api/v1/sources/${id}`} target="_blank" rel="noreferrer">
            {" "}source {i + 1}
          </a>
        ))}
      </p>
      {open ? (
        <div className="row">
          <button onClick={() => snooze("1h")} disabled={busy}>Snooze 1 h</button>
          <button onClick={() => snooze("tomorrow")} disabled={busy}>Tomorrow</button>
          <button onClick={() => command("acted")} disabled={busy}>Done</button>
          <button onClick={() => command("dismiss")} disabled={busy}>Dismiss</button>
        </div>
      ) : (
        <p className="meta">{reminder.state}</p>
      )}
      {error && <p className="error">{error}</p>}
    </li>
  );
}
