"use client";

import { useCallback, useEffect, useState } from "react";
import { ReminderRow } from "@/components/ReminderRow";
import { api, unwrap } from "@/lib/api";
import { enablePush, pushSupported } from "@/lib/push";
import type { AppNotification, Page, Reminder } from "@/lib/types";

export default function RemindersPage() {
  const [reminders, setReminders] = useState<Reminder[]>([]);
  const [notifications, setNotifications] = useState<AppNotification[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [push, setPush] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const active = unwrap<Page<Reminder>>(await api.GET("/api/v1/reminders", { params: { query: { state: "active" } } }));
      const center = unwrap<Page<AppNotification>>(await api.GET("/api/v1/notifications", { params: { query: {} } }));
      setReminders(active.items);
      setNotifications(center.items);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load reminders");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function markAllRead() {
    try {
      unwrap(await api.POST("/api/v1/notifications/read", { body: { before: new Date().toISOString() } }));
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not mark notifications read");
    }
  }

  async function turnOnPush() {
    setPush(await enablePush());
  }

  const replace = (next: Reminder) =>
    setReminders((prev) =>
      next.state === "delivered" || next.state === "snoozed"
        ? prev.map((r) => (r.id === next.id ? next : r))
        : prev.filter((r) => r.id !== next.id),
    );

  const unread = notifications.filter((n) => !n.read_at).length;

  return (
    <>
      <h1>Reminders</h1>
      {error && <p className="error">{error}</p>}
      {reminders.length === 0 ? (
        <p className="empty">No active reminders.</p>
      ) : (
        <ul>
          {reminders.map((r) => (
            <ReminderRow key={r.id} reminder={r} onChange={replace} />
          ))}
        </ul>
      )}

      <h2>Notifications{unread > 0 && ` (${unread} unread)`}</h2>
      <div className="row">
        {unread > 0 && <button onClick={markAllRead}>Mark all read</button>}
        {pushSupported() && <button onClick={turnOnPush}>Enable browser notifications</button>}
      </div>
      {push && <p className="meta">{push}</p>}
      {notifications.length === 0 ? (
        <p className="empty">No notifications.</p>
      ) : (
        <ul style={{ marginTop: 8 }}>
          {notifications.map((n) => (
            <li key={n.id} className="card">
              <p className={n.read_at ? "meta" : "title"}>{n.reminder.text}</p>
              <p className="meta">{new Date(n.created_at).toLocaleString()}</p>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
