// Chat over Server-Sent Events (BACKEND_DESIGN.md §16.7). EventSource cannot POST, so the stream
// is read with fetch. The request body type comes from the generated client (lib/schema.d.ts).
import type { components } from "./schema";
import type { ChatEvent } from "./types";

export type PostMessageBody = components["schemas"]["PostMessage"];

function csrfToken(): string | undefined {
  if (typeof document === "undefined") return undefined;
  return document.cookie
    .split("; ")
    .find((c) => c.startsWith("eca_csrf="))
    ?.slice("eca_csrf=".length);
}

function parseBlock(block: string): ChatEvent | null {
  let name = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) name = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trim());
  }
  if (data.length === 0) return null;
  return { name, data: JSON.parse(data.join("\n")) } as ChatEvent;
}

/** POST a question and yield the stream's events (plan, sources, delta, final or error). */
export async function* askQuestion(
  sessionId: string,
  body: PostMessageBody,
  idempotencyKey: string,
): AsyncGenerator<ChatEvent> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
    "Idempotency-Key": idempotencyKey,
  };
  const token = csrfToken();
  if (token) headers["X-CSRF-Token"] = token;
  const response = await fetch(`/api/v1/chat/sessions/${sessionId}/messages`, {
    method: "POST",
    credentials: "same-origin",
    headers,
    body: JSON.stringify(body),
  });
  if (!response.ok || !response.body) {
    let detail = `HTTP ${response.status}`;
    try {
      const problem = (await response.json()) as { detail?: string; title?: string };
      detail = problem.detail ?? problem.title ?? detail;
    } catch {
      // not a Problem Details body
    }
    yield { name: "error", data: { code: String(response.status), title: detail } };
    return;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let index = buffer.indexOf("\n\n");
    while (index >= 0) {
      const event = parseBlock(buffer.slice(0, index));
      buffer = buffer.slice(index + 2);
      if (event) yield event;
      index = buffer.indexOf("\n\n");
    }
  }
}
