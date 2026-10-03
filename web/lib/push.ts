// Web Push enrollment (slice 3.1, TECHNICAL_DESIGN.md §15.4): register the service worker,
// subscribe with the server's VAPID public key and send the subscription to the API. Pushes carry
// no payload, so only the endpoint is stored server-side.
import { ApiError, api, unwrap } from "./api";

export function pushSupported(): boolean {
  return typeof window !== "undefined" && "serviceWorker" in navigator && "PushManager" in window;
}

function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padded = (base64 + "=".repeat((4 - (base64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(padded);
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

/** Returns a short status message for the page. */
export async function enablePush(): Promise<string> {
  if (!pushSupported()) return "This browser does not support push notifications.";
  try {
    const { public_key } = unwrap<{ public_key: string }>(await api.GET("/api/v1/push-subscriptions/key"));
    const permission = await Notification.requestPermission();
    if (permission !== "granted") return "Notifications were not allowed.";
    const registration = await navigator.serviceWorker.register("/sw.js");
    const subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(public_key),
    });
    const json = subscription.toJSON();
    unwrap(
      await api.POST("/api/v1/push-subscriptions", {
        body: { endpoint: json.endpoint ?? "", expirationTime: json.expirationTime ?? null },
      }),
    );
    return "Browser notifications are on.";
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return "Push notifications are not configured on this server.";
    return e instanceof Error ? e.message : "Could not enable notifications.";
  }
}
