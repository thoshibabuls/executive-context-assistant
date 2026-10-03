"""Answer rendering (AI_PIPELINE.md §5.7-§5.8; CONTEXT_ARCHITECTURE.md §10.7-§10.10). Pure functions.

- Deterministic list answers: one line per packed item with its citation, grouped by person when
  a list has more than 7 entries; claim kinds from the items' data class (no AI call).
- Model answers: rendered from the verified claims only, labelled by kind ("Inferred:",
  "Suggestion:", "You confirmed:"), with citation chips; the model's own markdown is not shown.
- Abstention and degraded templates with the coverage sentence; disclosure of coverage gaps.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from eca.chat.grounding import Verified, VerifiedClaim
from eca.retrieval import COVERAGE_CID, Assembly, PacketItem, coverage_sentence

ABSTAIN = "The available context does not establish this."
DEGRADED = "A written answer is not available right now."
UNSUPPORTED = (
    "I can answer questions about your email, meetings, commitments, tasks, deadlines, people, projects and "
    "what changed. I cannot do that request."
)
GROUP_THRESHOLD = 7
HEADINGS = {
    "waiting_for": "Waiting for",
    "promised": "What you promised",
    "who_waiting_on_me": "Who is waiting on you",
    "needs_response": "Needs your response",
    "overdue": "Overdue",
    "deadlines": "Deadlines",
    "day_view": "What happened",
    "what_changed": "What changed",
    "email_context": "This thread",
    "next_action": "Your open priorities",
    "person": "Context",
    "topic_status": "Related items",
    "project": "Project context",
    "meeting_lookup": "From this meeting",
}
EMPTY = {
    "waiting_for": "Nobody owes you an open item that I can find.",
    "promised": "No open commitment of yours was found.",
    "who_waiting_on_me": "Nobody is waiting on you, as far as the synced sources show.",
    "needs_response": "No thread is waiting for your reply.",
    "overdue": "Nothing is overdue.",
    "deadlines": "No deadline falls in this period.",
    "day_view": "No material activity was recorded for that day.",
    "what_changed": "No material change was recorded.",
    "meeting_lookup": "No action items, decisions or open questions were found for this meeting.",
}
KIND_PREFIX = {
    "inference": "Inferred: ",
    "recommendation": "Suggestion: ",
    "user": "You confirmed: ",
    "absence": "",
}


@dataclass(frozen=True)
class Rendered:
    text: str
    claims: list[dict[str, Any]]
    cited: list[str]  # citation IDs used


def _chips(citations: tuple[str, ...] | list[str]) -> str:
    return "".join(f" [{c}]" for c in citations)


def claim_line(c: VerifiedClaim) -> str:
    return f"{KIND_PREFIX.get(c.kind, '')}{c.text}{_chips(c.citations)}"


def _claim_json(
    text: str, citations: list[str], kind: str, *, flagged: bool = False, reason: str | None = None
) -> dict[str, Any]:
    return {"text": text, "citations": citations, "kind": kind, "flagged": flagged, "reason": reason}


def _gap_note(assembly: Assembly) -> str | None:
    """Stale or disconnected sources are always disclosed (§9.4)."""
    if assembly.coverage.has_gaps:
        return "Note: " + coverage_sentence(assembly.coverage)
    return None


def _list_items(assembly: Assembly) -> list[PacketItem]:
    return [i for i in assembly.packet.items if i.section in ("anchors", "state") and i.kind != "person"]


def render_list(assembly: Assembly, intent: str) -> Rendered:
    """Deterministic list answer (S7-S10, S1 panel, overdue, deadlines; AI_PIPELINE.md §5.8)."""
    items = _list_items(assembly)
    lines: list[str] = []
    claims: list[dict[str, Any]] = []
    cited: list[str] = []
    window = (
        f" ({assembly.window.label})"
        if assembly.window is not None and intent in ("day_view", "deadlines", "what_changed")
        else ""
    )
    if not items:
        sentence = EMPTY.get(intent, "Nothing matching was found.")
        text = f"{sentence} {coverage_sentence(assembly.coverage)}"
        claims.append(_claim_json(sentence, [COVERAGE_CID], "absence"))
        lines.append(f"{text} [{COVERAGE_CID}]")
        cited.append(COVERAGE_CID)
    else:
        lines.append(f"**{HEADINGS.get(intent, 'Results')}{window}** ({len(items)})")
        grouped: OrderedDict[str, list[PacketItem]] = OrderedDict()
        use_groups = len(items) > GROUP_THRESHOLD or intent in (
            "day_view",
            "what_changed",
            "who_waiting_on_me",
        )
        for item in items:
            key = (item.group or "other") if use_groups else ""
            grouped.setdefault(key, []).append(item)
        for group, members in grouped.items():
            if group:
                lines.append(f"\n*{group}*")
            for item in members:
                assert item.cid is not None
                lines.append(f"- {item.line} [{item.cid}]")
                claims.append(_claim_json(item.line, [item.cid], item.claim_kind))
                cited.append(item.cid)
    for note in assembly.notes:
        lines.append(f"\n_{note}_")
    gap = _gap_note(assembly)
    if gap and items:
        lines.append(f"\n{gap} [{COVERAGE_CID}]")
        cited.append(COVERAGE_CID)
    return Rendered("\n".join(lines), claims, cited)


def render_verified(assembly: Assembly, verified: Verified, *, notice: str | None = None) -> Rendered:
    lines = [claim_line(c) for c in verified.claims]
    cited = sorted({c for claim in verified.claims for c in claim.citations})
    if verified.needs_coverage_sentence:
        lines.append(coverage_sentence(assembly.coverage) + f" [{COVERAGE_CID}]")
    gap = _gap_note(assembly)
    if gap and not verified.needs_coverage_sentence:
        lines.append(f"{gap} [{COVERAGE_CID}]")
        cited.append(COVERAGE_CID)
    for note in assembly.notes:
        lines.append(f"_{note}_")
    if notice:
        lines.append(f"_{notice}_")
    claims = [
        _claim_json(c.text, list(c.citations), c.kind, flagged=c.flagged, reason=c.reason)
        for c in verified.claims
    ]
    return Rendered("\n\n".join(lines), claims, sorted(set(cited)))


def render_abstention(
    assembly: Assembly, *, missing_info: str | None = None, clarification: str | None = None
) -> Rendered:
    """§5.7 abstention template: the sentence, the coverage, up to 3 closest related items."""
    if clarification:
        return Rendered(clarification, [], [])
    lines = [ABSTAIN, coverage_sentence(assembly.coverage) + f" [{COVERAGE_CID}]"]
    if missing_info:
        lines.append(f"Missing: {missing_info}")
    for name in assembly.unresolved:
        lines.append(f'No person named "{name}" was found in your contacts.')
    related = [i for i in assembly.related if i.cid]
    if related:
        lines.append("Closest related:")
        lines += [f"- {i.line} [{i.cid}]" for i in related]
    claims = [_claim_json(ABSTAIN, [COVERAGE_CID], "absence")]
    return Rendered("\n".join(lines), claims, [COVERAGE_CID, *[i.cid for i in related if i.cid]])


def render_degraded(assembly: Assembly, reason: str, intent: str) -> Rendered:
    """§14 degraded route: the deterministic list of retrieved items with a notice."""
    listed = render_list(assembly, intent)
    return Rendered(f"{DEGRADED} {reason}\n\n{listed.text}", listed.claims, listed.cited)


def render_unsupported() -> Rendered:
    return Rendered(UNSUPPORTED, [], [])


def citation_snapshots(assembly: Assembly, cited: list[str]) -> list[dict[str, Any]]:
    """Text and source references of every cited item, kept with the answer (BACKEND_DESIGN.md §8.5)."""
    by_cid = assembly.packet.by_cid()
    out: list[dict[str, Any]] = []
    for cid in cited:
        if cid == COVERAGE_CID:
            out.append(
                {"cid": cid, "kind": "coverage", "text": assembly.coverage.text, "source_item_ids": []}
            )
            continue
        item = by_cid.get(cid)
        if item is None:
            continue
        out.append(
            {
                "cid": cid,
                "kind": item.kind,
                "entity_id": str(item.entity_id) if item.entity_id else None,
                "text": item.line,
                "data_class": item.data_class,
                "source_item_ids": [str(s) for s in item.source_item_ids],
                "evidence_ids": [str(e) for e in item.evidence_ids],
            }
        )
    return out
