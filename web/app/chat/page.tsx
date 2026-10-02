"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError, api, newIdempotencyKey, unwrap } from "@/lib/api";
import { askQuestion } from "@/lib/chat";
import type { ChatMessage, ChatSession, Citation, Claim, Page } from "@/lib/types";

// Provenance labels (AI_PIPELINE.md §5.7): source facts, your own data, inferences and suggestions
// are shown differently; an absence statement always comes with what was searched.
const KIND_LABEL: Record<Claim["kind"], string> = {
  source: "From your sources",
  user: "You confirmed",
  inference: "Inferred",
  recommendation: "Suggestion",
  absence: "Not found",
};

const TIER_LABEL: Record<string, string> = {
  deterministic: "Listed from your data (no AI)",
  T1: "AI answer (lookup), checked against sources",
  T2: "AI answer (synthesis), checked against sources",
  abstain: "Not enough evidence to answer",
  degraded: "AI unavailable: retrieved items only",
};

function CitationList({ citations }: { citations: Citation[] }) {
  if (citations.length === 0) return null;
  return (
    <ul className="citations">
      {citations.map((c) => (
        <li key={c.cid} className="meta">
          <strong>[{c.cid}]</strong> {c.text}
          {c.data_class === "ai_derived" && <span className="label ai"> · AI-derived</span>}
          {c.source_item_ids.map((id, i) => (
            <a key={id} href={`/api/v1/sources/${id}`} target="_blank" rel="noreferrer">
              {" "}
              source {i + 1}
            </a>
          ))}
        </li>
      ))}
    </ul>
  );
}

function Answer({ message }: { message: ChatMessage }) {
  return (
    <li className="card">
      <p className="meta">
        {TIER_LABEL[message.answer_tier ?? ""] ?? "Answer"}
        {message.confidence && ` · ${message.confidence} confidence`}
      </p>
      {message.claims.length > 0 ? (
        <ul>
          {message.claims.map((c, i) => (
            <li key={i}>
              <span className={c.kind === "source" || c.kind === "user" ? "label" : "label ai"}>{KIND_LABEL[c.kind]}</span>{" "}
              {c.text} {c.citations.map((id) => `[${id}]`).join(" ")}
              {c.flagged && <span className="label warn"> · not confirmed by the cited sources</span>}
            </li>
          ))}
        </ul>
      ) : (
        <p style={{ whiteSpace: "pre-wrap" }}>{message.content}</p>
      )}
      {message.claims.length > 0 && <p className="meta" style={{ whiteSpace: "pre-wrap" }}>{message.content}</p>}
      <CitationList citations={message.citations} />
    </li>
  );
}

export default function ChatPage() {
  const [session, setSession] = useState<ChatSession | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [text, setText] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const page = unwrap<Page<ChatSession>>(await api.GET("/api/v1/chat/sessions", { params: { query: { limit: 1 } } }));
      const current =
        page.items[0] ?? unwrap<ChatSession>(await api.POST("/api/v1/chat/sessions", { body: { scope: { kind: "global" } } }));
      const full = unwrap<ChatSession>(
        await api.GET("/api/v1/chat/sessions/{session_id}", { params: { path: { session_id: current.id } } }),
      );
      setSession(full);
      setMessages(full.messages ?? []);
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) {
        window.location.href = "/api/v1/auth/google/login";
        return;
      }
      setError(e instanceof Error ? e.message : "Could not load the chat");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function send() {
    const question = text.trim();
    if (!session || !question || busy) return;
    setBusy(true);
    setError(null);
    setStatus("Planning…");
    const pending: ChatMessage = {
      id: `pending-${Date.now()}`,
      session_id: session.id,
      role: "user",
      content: question,
      reply_to_id: null,
      scenario: null,
      answer_tier: null,
      claims: [],
      citations: [],
      confidence: null,
      provenance: null,
      created_at: null,
    };
    setMessages((prev) => [...prev, pending]);
    setText("");
    try {
      for await (const event of askQuestion(session.id, { text: question }, newIdempotencyKey())) {
        if (event.name === "plan") setStatus(`Looking up (${event.data.scenario})…`);
        else if (event.name === "sources") setStatus(`Checking ${event.data.citations.length} items…`);
        else if (event.name === "final") setMessages((prev) => [...prev, event.data.message]);
        else if (event.name === "error") setError(event.data.title);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "The answer could not be loaded");
    } finally {
      setStatus(null);
      setBusy(false);
    }
  }

  if (!session) return error ? <p className="error">{error}</p> : <p className="empty">Loading…</p>;

  return (
    <>
      <h1>Ask about your work</h1>
      <p className="meta">
        Answers cite your email, calendar and tasks. AI inferences and suggestions are labelled; when the
        evidence is not there, the assistant says so.
      </p>
      <ul>
        {messages.map((m) =>
          m.role === "user" ? (
            <li key={m.id} className="card">
              <p className="title">{m.content}</p>
            </li>
          ) : (
            <Answer key={m.id} message={m} />
          ),
        )}
      </ul>
      {status && <p className="meta">{status}</p>}
      {error && <p className="error">{error}</p>}
      <div className="row">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void send();
          }}
          placeholder="What am I waiting for?"
          aria-label="Question"
          maxLength={2000}
          style={{ flex: 1 }}
        />
        <button onClick={() => void send()} disabled={busy || !text.trim()}>
          Ask
        </button>
      </div>
    </>
  );
}
