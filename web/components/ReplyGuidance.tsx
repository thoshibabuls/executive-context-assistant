"use client";

import { useState } from "react";
import { newIdempotencyKey } from "@/lib/api";
import { type Guidance, requestGuidance } from "@/lib/guidance";
import type { Claim } from "@/lib/types";

const KIND_LABEL: Record<string, string> = {
  source: "From your sources",
  user: "You confirmed",
  inference: "Inferred",
  recommendation: "Suggestion",
  absence: "Not found",
};

function Claims({ title, claims }: { title: string; claims: Claim[] }) {
  if (claims.length === 0) return null;
  return (
    <>
      <p className="title">{title}</p>
      <ul>
        {claims.map((c, i) => (
          <li key={i} className="meta">
            <span className={c.kind === "inference" || c.kind === "recommendation" ? "label ai" : "label"}>
              {KIND_LABEL[c.kind] ?? c.kind}
            </span>{" "}
            {c.text}
            {c.citations.length > 0 && <span className="tag"> [{c.citations.join(", ")}]</span>}
            {c.flagged && <span className="label warn"> ({c.reason})</span>}
          </li>
        ))}
      </ul>
    </>
  );
}

/** Reply guidance for one thread: context, previous agreement, current status, copy-only draft. */
export function ReplyGuidance({ conversationId }: { conversationId: string }) {
  const [instructions, setInstructions] = useState("");
  const [guidance, setGuidance] = useState<Guidance | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  async function ask() {
    setBusy(true);
    setError(null);
    setCopied(false);
    try {
      setGuidance(await requestGuidance(conversationId, instructions.trim() || null, newIdempotencyKey()));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not prepare guidance");
    } finally {
      setBusy(false);
    }
  }

  async function copy() {
    if (!guidance?.draft) return;
    await navigator.clipboard.writeText(guidance.draft.text);
    setCopied(true);
  }

  return (
    <div className="card">
      <div className="row">
        <input
          value={instructions}
          onChange={(e) => setInstructions(e.target.value)}
          placeholder="Optional: how should the reply go? (e.g. decline politely)"
          aria-label="Reply instructions"
          maxLength={500}
        />
        <button onClick={ask} disabled={busy}>
          {busy ? "Preparing…" : "Help me reply"}
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      {guidance && (
        <>
          {guidance.notice && <p className="label warn">{guidance.notice}</p>}
          <Claims title="Context" claims={guidance.sections.context} />
          <Claims title="Previous agreement" claims={guidance.sections.previous_agreement} />
          <Claims title="Current status" claims={guidance.sections.current_status} />
          {guidance.missing_info && <p className="meta">Missing: {guidance.missing_info}</p>}
          {guidance.draft && (
            <>
              <p className="title">Suggested response</p>
              <p className="label ai">{guidance.draft.label}</p>
              <pre style={{ whiteSpace: "pre-wrap", font: "inherit" }}>{guidance.draft.text}</pre>
              {guidance.draft.warnings.length > 0 && (
                <p className="label warn">Not found in the sources: {guidance.draft.warnings.join(", ")}</p>
              )}
              <button onClick={copy}>{copied ? "Copied" : "Copy draft"}</button>
            </>
          )}
          {guidance.citations.length > 0 && (
            <ul className="citations">
              {guidance.citations.map((c) => (
                <li key={c.cid} className="meta">
                  [{c.cid}] {c.text}
                  {c.source_item_ids.map((id, i) => (
                    <a key={id} href={`/api/v1/sources/${id}`} target="_blank" rel="noreferrer">
                      {" "}source {i + 1}
                    </a>
                  ))}
                </li>
              ))}
            </ul>
          )}
          <p className="meta">Coverage: {guidance.coverage}</p>
        </>
      )}
    </div>
  );
}
