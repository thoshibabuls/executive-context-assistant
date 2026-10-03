// Reply guidance over Server-Sent Events (slice 3.3, BACKEND_DESIGN.md §16.8). The draft is
// copy-only: nothing is ever sent for the user.
import type { Citation, Claim } from "./types";

export type Guidance = {
  conversation_id: string;
  tier: "T2" | "abstain" | "degraded";
  notice: string | null;
  sections: { context: Claim[]; previous_agreement: Claim[]; current_status: Claim[] };
  draft: { text: string; kind: "recommendation"; label: string; warnings: string[] } | null;
  confidence: "low" | "medium" | "high" | null;
  missing_info: string | null;
  citations: Citation[];
  coverage: string;
};

type GuidanceEvent =
  | { name: "sources"; data: { citations: Citation[]; coverage?: string } }
  | { name: "final"; data: { guidance: Guidance } }
  | { name: "error"; data: { code: string; title: string } };

function csrfToken(): string | undefined {
  if (typeof document === "undefined") return undefined;
  return document.cookie
    .split("; ")
    .find((c) => c.startsWith("eca_csrf="))
    ?.slice("eca_csrf=".length);
}

/** POST the request and return the final guidance, or throw with the server message. */
export async function requestGuidance(
  conversationId: string,
  instructions: string | null,
  idempotencyKey: string,
): Promise<Guidance> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
    "Idempotency-Key": idempotencyKey,
  };
  const token = csrfToken();
  if (token) headers["X-CSRF-Token"] = token;
  const response = await fetch(`/api/v1/conversations/${conversationId}/reply-guidance`, {
    method: "POST",
    credentials: "same-origin",
    headers,
    body: JSON.stringify({ instructions }),
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const problem = (await response.json()) as { detail?: string; title?: string };
      detail = problem.detail ?? problem.title ?? detail;
    } catch {
      // not a Problem Details body
    }
    throw new Error(detail);
  }
  const text = await response.text();
  for (const block of text.split("\n\n")) {
    let name = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) name = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trim());
    }
    if (data.length === 0) continue;
    const event = { name, data: JSON.parse(data.join("\n")) } as GuidanceEvent;
    if (event.name === "final") return event.data.guidance;
    if (event.name === "error") throw new Error(event.data.title);
  }
  throw new Error("The guidance stream ended without a result.");
}
