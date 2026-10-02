// Response shapes of the routes the pages use (backend/eca/api/work.py and context.py). The
// generated lib/schema.d.ts types paths and parameters; these bodies are untyped there because
// the routes return plain dicts.

export type Provenance = {
  source: "user" | "ai_inference";
  confidence: number | null;
  confidence_band: "low" | "medium" | "high" | null;
  derived_at: string;
  extraction_method: string;
  extraction_id: string | null;
  model: string | null;
  evidence_source_ids: string[];
};

export type PriorityReason = { feature: string; text: string; weight?: number };

export type WorkItem = {
  id: string;
  type: string;
  title: string;
  direction: string;
  owner_person_id: string | null;
  counterparty_person_id: string | null;
  requester_person_id: string | null;
  due_at: string | null;
  due_precision: string | null;
  due_text: string | null;
  lifecycle_status: "open" | "in_progress" | "done" | "cancelled";
  verification_status: "suggested" | "confirmed" | "rejected" | "user_created";
  origin: "ai" | "user";
  has_conflict: boolean;
  has_source_gap: boolean;
  notes: string | null;
  priority: { score: number | null; reasons: PriorityReason[] };
  version: number;
  provenance: Provenance;
};

export type Conversation = {
  id: string;
  subject: string | null;
  needs_reply_reason: string | null;
  last_inbound_at: string | null;
  latest_snippet: string | null;
  priority: { score: number | null; reasons: PriorityReason[]; override: number | null };
  triage: Record<string, unknown> | null;
  version: number;
};

export type Person = { id: string; display_name: string | null; primary_email: string | null };

export type Meeting = {
  id: string;
  title: string | null;
  starts_at: string;
  ends_at: string;
  status: string;
  conference_uri: string | null;
  attendee_ids: string[];
};

export type Today = {
  date: string;
  timezone: string;
  attention: ({ kind: "work_item"; item: WorkItem } | { kind: "conversation"; conversation: Conversation })[];
  commitments: WorkItem[];
  waiting_for: WorkItem[];
  deadlines: WorkItem[];
  new_suggestions: WorkItem[];
  needs_response: Conversation[];
  meetings: Meeting[];
  people: Record<string, Person>;
};

export type Page<T> = { items: T[]; next_cursor: string | null };
