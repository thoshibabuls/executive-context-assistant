"""AI-11 suggested asks for meeting prep (slice 4.4, AI_PIPELINE.md §5.10; BACKEND_DESIGN.md §15).

Natural-key job on ``MeetingAsksRequested`` (queue ``ai_standard``). Transaction 1, under
``asks:{meeting}:{version}``: the stored sections must still be that version with asks
``pending``; at the soft budget cap the asks are ``skipped`` without a call. The model is called
outside any transaction. Transaction 2 stores the result only while the version still matches
(``meetings.store_prep_asks``). Every ask is a recommendation that must cite the section lines it
rests on; asks with unknown or missing citations are dropped. Logs carry IDs only.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text

from eca import intelligence, meetings
from eca.intelligence import AIClient, BudgetLevel
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory
from eca.retrieval.cards import delimit
from eca.retrieval.chunking import estimate_tokens

log = structlog.get_logger("eca.retrieval.asks")

ASKS_DYNAMIC_TOKENS = 2000
ASKS_HARD_TOKENS = 4000
_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended('asks:' || :meeting || ':' || :version, 0))")
LIST_SECTIONS = (
    ("what_changed", "changed since the last meeting"),
    ("unresolved_questions", "unresolved question"),
    ("deadlines", "deadline before the meeting"),
    ("open_items_theirs", "owed to the user"),
    ("open_items_mine", "owed by the user"),
)


@dataclass(frozen=True)
class SectionLine:
    cid: str
    section: str
    entity_id: str | None
    text: str


def _item_text(entry: dict[str, Any]) -> str:
    bits = [delimit(entry.get("title") or entry.get("statement") or "")]
    if entry.get("owner"):
        bits.append(f"owner {delimit(entry['owner'])}")
    if entry.get("counterparty"):
        bits.append(f"with {delimit(entry['counterparty'])}")
    if entry.get("due_text") or entry.get("due_at"):
        bits.append(f"due {delimit(entry.get('due_text') or entry.get('due_at'))}")
    if entry.get("kind"):
        bits.append(str(entry["kind"]))
    if entry.get("verification_status") == "suggested":
        bits.append("AI suggestion")
    return " · ".join(bits)


def section_lines(sections: dict[str, Any]) -> list[SectionLine]:
    """The prep sections as cited lines ``S1``...``Sn`` within the 4 K hard cap (2 K dynamic)."""
    lines: list[SectionLine] = []
    purpose = sections.get("purpose") or {}
    head = [f"meeting {delimit(purpose.get('title') or '(untitled)')}"]
    if purpose.get("description"):
        head.append(f"agenda {delimit(purpose['description'])}")
    if purpose.get("attendees"):
        head.append(f"attendees {delimit(', '.join(purpose['attendees']))}")
    lines.append(SectionLine("S1", "purpose", None, "[purpose] " + " · ".join(head)))
    tokens = estimate_tokens(lines[0].text)
    for key, label in LIST_SECTIONS:
        for entry in sections.get(key) or []:
            line = f"[{label}] {_item_text(entry)}"
            cost = estimate_tokens(line)
            if tokens + cost > ASKS_HARD_TOKENS or (tokens + cost > ASKS_DYNAMIC_TOKENS and len(lines) >= 8):
                return lines
            lines.append(
                SectionLine(f"S{len(lines) + 1}", key, entry.get("id") or entry.get("entity_id"), line)
            )
            tokens += cost
    return lines


def render_lines(lines: list[SectionLine]) -> str:
    return "\n".join(f"[{line.cid}] {line.text}" for line in lines)


def keep_cited(asks: list[Any], lines: list[SectionLine]) -> list[dict[str, Any]]:
    """Drop asks without a valid citation; unknown citation IDs are removed (AI_PIPELINE.md §5.10)."""
    by_cid = {line.cid: line for line in lines}
    out: list[dict[str, Any]] = []
    for ask in asks:
        cids = [c for c in ask.citations if c in by_cid]
        if not cids:
            continue
        out.append(
            {
                "text": ask.text,
                "kind": "recommendation",
                "citations": cids,
                "sources": [{"section": by_cid[c].section, "entity_id": by_cid[c].entity_id} for c in cids],
            }
        )
    return out


async def _lock(uow: UnitOfWork, meeting_id: UUID, version: int) -> None:
    await uow.session.execute(_LOCK_SQL, {"meeting": str(meeting_id), "version": str(version)})


async def generate_asks(
    factory: UnitOfWorkFactory,
    client: AIClient,
    *,
    user_id: UUID,
    meeting_id: UUID,
    version: int,
    now: datetime.datetime,
) -> str:
    """``skipped`` | ``ready`` | ``failed`` | ``stale``."""
    async with factory(user_id=user_id) as uow:
        await _lock(uow, meeting_id, version)
        prep = await meetings.get_prep(uow, meeting_id)
        if prep is None or prep.version != version or prep.asks["status"] != "pending":
            return "stale"
        lines = section_lines(prep.sections)
        if len(lines) <= 1:
            await meetings.store_prep_asks(
                uow, meeting_id, version=version, status="skipped", items=[], provenance=None
            )
            return "skipped"
        level = await intelligence.budget_level(uow, now=now)
        if level is not BudgetLevel.OK:  # AI-11 stops at the soft cap: sections only
            await meetings.store_prep_asks(
                uow, meeting_id, version=version, status="skipped", items=[], provenance={"reason": "budget"}
            )
            return "skipped"
    call = await intelligence.run_meeting_asks(client, render_lines(lines), user_id=user_id)
    items = keep_cited(list(call.output.asks), lines) if call.output is not None else []
    status = "ready" if items else "failed"
    provenance = {
        "source": "ai_inference",
        "role": "meeting_asks",
        "model": call.model,
        "prompt_version": call.prompt_version,
        "derived_at": now.isoformat(),
        "call_ids": [str(c) for c in call.call_ids],
        "degraded": call.degraded,
    }
    async with factory(user_id=user_id) as uow:
        await _lock(uow, meeting_id, version)
        stored = await meetings.store_prep_asks(
            uow, meeting_id, version=version, status=status, items=items, provenance=provenance
        )
    log.info("meeting_asks", meeting_id=str(meeting_id), version=version, status=status, stored=stored)
    return status if stored else "stale"
