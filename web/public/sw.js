// Web Push service worker (slice 3.1, TECHNICAL_DESIGN.md §15.4). Pushes carry no payload: on a
// push the worker fetches the newest unread notifications from the API (same origin, session
// cookie) and shows them, so no content passes through the push service.
self.addEventListener("push", (event) => {
  event.waitUntil(
    (async () => {
      let title = "Reminder";
      let body = "You have a new reminder.";
      let tag = "eca-reminder";
      try {
        const response = await fetch("/api/v1/notifications?unread=true&limit=1", { credentials: "same-origin" });
        if (response.ok) {
          const page = await response.json();
          const latest = page.items && page.items[0];
          if (latest) {
            body = latest.reminder.text;
            tag = latest.reminder.id;
          }
        }
      } catch (e) {
        // offline or signed out: show the generic notification
      }
      await self.registration.showNotification(title, { body, tag, data: { url: "/reminders" } });
    })(),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(self.clients.openWindow((event.notification.data && event.notification.data.url) || "/reminders"));
});
