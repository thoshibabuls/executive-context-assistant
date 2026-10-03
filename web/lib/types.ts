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
  reminders: Reminder[];
};

export type Page<T> = { items: T[]; next_cursor: string | null };

// Chat (backend/eca/chat, BACKEND_DESIGN.md §16.7). Claim kinds follow AI_PIPELINE.md §5.7.
export type ClaimKind = "source" | "user" | "inference" | "recommendation" | "absence";

export type Claim = { text: string; citations: string[]; kind: ClaimKind; flagged: boolean; reason: string | null };

export type Citation = {
  cid: string;
  kind: string;
  entity_id?: string | null;
  text: string;
  data_class?: string;
  source_item_ids: string[];
  evidence_ids?: string[];
};

export type ChatMessage = {
  id: string;
  session_id: string;
  role: "user" | "assistant";
  content: string;
  reply_to_id: string | null;
  scenario: string | null;
  answer_tier: "deterministic" | "T1" | "T2" | "abstain" | "degraded" | null;
  claims: Claim[];
  citations: Citation[];
  confidence: "low" | "medium" | "high" | null;
  provenance: { extraction_method?: string; model?: string | null; planner?: string } | null;
  created_at: string | null;
};

export type ChatSession = {
  id: string;
  title: string | null;
  scope: { kind: string; meeting_id?: string };
  last_active_at: string | null;
  messages?: ChatMessage[];
};

export type ChatEvent =
  | { name: "plan"; data: { scenario: string; tier: string; planner: string } }
  | { name: "sources"; data: { citations: Citation[]; coverage?: string } }
  | { name: "delta"; data: { text: string } }
  | { name: "final"; data: { message: ChatMessage } }
  | { name: "error"; data: { code: string; title: string } };

// People (slice 3.4, BACKEND_DESIGN.md §16.8). Every profile field states its origin: "user"
// (you set it), "computed" (counted from your mail, meetings and items) or "inferred" (AI-derived).
export type Origin = "user" | "computed" | "inferred" | null;

export type PersonProfile = {
  origin: "computed";
  computed_at: string | null;
  open_mine: number;
  open_theirs: number;
  inbound_30d: number;
  outbound_30d: number;
  meetings_30d: number;
  interaction_recency_days: number | null;
  active_topics: { text: string; origin: "inferred" | "computed" }[];
  last_meeting_at: string | null;
  next_meeting_at: string | null;
};

export type PersonRow = {
  id: string;
  display_name: string | null;
  primary_email: string | null;
  last_interaction_at: string | null;
  version: number;
  user_fields: string[];
  importance: { user: number | null; inferred: number | null; origin: Origin };
  relationship_type: { value: string | null; origin: Origin };
  role_title: { value: string | null; origin: Origin };
  profile: PersonProfile | null;
};

export type ContextCard = {
  kind: string;
  entity_id: string | null;
  line: string;
  data_class: string;
  claim_kind: string;
  user_backed: boolean;
  source_item_ids: string[];
  group: string | null;
};

export type PersonContext = PersonRow & {
  redirected_from: string | null;
  organization: { id: string; name: string; domain: string | null } | null;
  card: ContextCard | null;
  they_owe_me: WorkItem[];
  i_owe_them: WorkItem[];
  other_items: WorkItem[];
  threads: ContextCard[];
  meetings: ContextCard[];
  evidence: ContextCard[];
  decisions: ContextCard[];
  notes: string[];
};

// Reminders and the notification center (slice 3.1). Reminders are deterministic rules
// (origin "computed"); item_provenance labels the underlying item (AI suggestion or yours).
export type Reminder = {
  id: string;
  item_type: "work_item" | "conversation" | "meeting";
  item_id: string;
  reminder_type: "deadline" | "overdue" | "commitment" | "waiting_for" | "follow_up" | "meeting_prep";
  state: "pending" | "delivered" | "snoozed" | "dismissed" | "acted" | "suppressed" | "cancelled";
  text: string;
  fire_at: string;
  delivered_at: string | null;
  snoozed_until: string | null;
  proactive: boolean | null;
  item_provenance: {
    origin: string;
    verification_status: string;
    confidence_band: string | null;
    evidence_source_ids: string[];
  } | null;
  version: number;
};

export type AppNotification = { id: string; created_at: string; read_at: string | null; reminder: Reminder };

// Daily briefing (slice 3.2): deterministic sections, no AI call. Entries keep their data class.
export type BriefingEntry = {
  kind: string;
  id: string;
  title: string;
  person: string | null;
  due_at?: string | null;
  due_local?: string | null;
  reasons: (string | null)[];
  data_class: string;
  label: string;
  source_item_ids: string[];
};

export type Briefing = {
  date: string;
  timezone: string;
  headline: string;
  generated_at: string;
  trigger: "schedule" | "on_demand";
  updated_since_briefing: { material_changes: number; since: string } | null;
  sections: {
    priorities: BriefingEntry[];
    meetings: { id: string; title: string; starts_at: string; local_time: string }[];
    waiting_on: BriefingEntry[];
    deadlines: BriefingEntry[];
    people: { id: string; name: string | null; reason: string }[];
    communication: {
      received: number;
      require_attention: number;
      with_action_items: number;
      with_deadlines: number;
      respond_today: number;
    };
  };
};
