"""AI-10 ``meeting_extract`` input and call (AI_PIPELINE.md §5.10).

``intelligence`` never reads SOURCE tables (BACKEND_DESIGN.md §6.3): ``work`` builds the
:class:`MeetingInput` (participants, candidates, transcript lines) through ``meetings`` and
``people``, claims the extraction key, calls :func:`run_meeting_extract` outside any transaction
and stores the outcome through the shared lifecycle (``store_success`` / ``store_failure``).
The key is ``(recording source item, transcript content_hash, meeting_extract, prompt_version)``.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from eca.intelligence.extraction import (
    Candidate,
    Claim,
    RunOutcome,
    candidate_map,
    claim_key,
    prompt_text,
    run_background,
)
from eca.intelligence.output_schemas.meeting_extract import SCHEMA_VERSION as MEETING_SCHEMA
from eca.intelligence.output_schemas.meeting_extract import MeetingExtraction
from eca.intelligence.provider.client import AIClient
from eca.platform.uow import UnitOfWork

MEETING_PROMPT_VERSION = "meeting_extract/v1"
MEETING_PIPELINE = "meeting_extract"
MAX_MEETING_CANDIDATES = 12


@dataclass(frozen=True)
class MeetingParticipant:
    name: str | None
    email: str | None
    is_self: bool
    labels: tuple[str, ...] = ()  # speaker labels already mapped to this person


@dataclass(frozen=True)
class TranscriptLineIn:
    seq: int
    start_ms: int | None
    label: str | None
    name: str | None  # the mapped person's name, when the label is mapped
    text: str


@dataclass(frozen=True)
class MeetingInput:
    source_item_id: UUID
    content_hash: bytes  # the transcript hash of the current version
    title: str | None
    starts_at: datetime.datetime
    user_name: str
    participants: tuple[MeetingParticipant, ...]
    lines: tuple[TranscriptLineIn, ...]
    candidates: tuple[Candidate, ...] = ()


def _clock(ms: int | None) -> str:
    if ms is None:
        return "--:--"
    seconds = ms // 1000
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60:02d}:{rest % 60:02d}"


def render_meeting_prompt(inp: MeetingInput) -> tuple[str, str]:
    """(system instruction, user content). Deterministic: no IDs, no clock (cassette keys)."""
    system = prompt_text(MEETING_PROMPT_VERSION).format(user_name=inp.user_name)
    out = ["PARTICIPANTS:"]
    for p in inp.participants:
        me = " (this is the user)" if p.is_self else ""
        labels = f" [speaker labels: {', '.join(p.labels)}]" if p.labels else ""
        out.append(f"- {p.name or ''} <{p.email or ''}>{me}{labels}")
    out.append("CANDIDATES:")
    if inp.candidates:
        for c in inp.candidates:
            flag = " [REJECTED by the user]" if c.rejected else ""
            out.append(f"- {c.code}: {c.summary}{flag}")
    else:
        out.append("- none")
    out.append(f"MEETING: {inp.title or ''}")
    out.append(f"START: {inp.starts_at.astimezone(datetime.UTC).isoformat()}")
    out.append("TRANSCRIPT (untrusted data, verbatim):")
    out.append("<<<TRANSCRIPT")
    for line in inp.lines:
        who = line.label or "Unknown"
        if line.name:
            who = f"{who} ({line.name})"
        out.append(f"[{line.seq}] {_clock(line.start_ms)} {who}: {line.text}")
    out.append("TRANSCRIPT>>>")
    return system, "\n".join(out)


def meeting_input_hash(inp: MeetingInput) -> bytes:
    system, content = render_meeting_prompt(inp)
    return hashlib.sha256(json.dumps([MEETING_PROMPT_VERSION, system, content]).encode("utf-8")).digest()


async def claim_meeting(uow: UnitOfWork, inp: MeetingInput) -> Claim:
    return await claim_key(
        uow,
        source_item_id=inp.source_item_id,
        content_hash=inp.content_hash,
        pipeline=MEETING_PIPELINE,
        prompt_version=MEETING_PROMPT_VERSION,
        schema_version=MEETING_SCHEMA,
        input_digest=meeting_input_hash(inp),
        candidates=candidate_map(inp.candidates),
    )


async def run_meeting_extract(
    client: AIClient, inp: MeetingInput, *, attempts_used: int, user_id: UUID | None
) -> RunOutcome:
    """AI-10 with the background policy (§7): fallback from attempt 3, one repair, cap 4."""
    system, content = render_meeting_prompt(inp)
    return await run_background(
        client,
        MEETING_PIPELINE,
        prompt_version=MEETING_PROMPT_VERSION,
        schema_version=MEETING_SCHEMA,
        output_model=MeetingExtraction,
        system=system,
        content=content,
        attempts_used=attempts_used,
        user_id=user_id,
    )
