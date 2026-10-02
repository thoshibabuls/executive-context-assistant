"""Extraction lifecycle (BACKEND_DESIGN.md §8.1) and AI-01 / AI-02 calls (AI_PIPELINE.md §5.2, §7).

``intelligence`` never reads SOURCE tables (§6.3): the caller (``work``, §5.6) builds an input DTO
with the message text, participants and candidates. The lifecycle has three steps, each a
separate call so the model call is never inside a database transaction:

1. ``claim`` (caller's transaction): insert the ``running`` row or load the existing one.
2. ``run_email_extract`` (no transaction): the model call with repair and fallback per §7.
3. ``store_success`` / ``store_failure`` (caller's transaction): output, status, outbox event.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from eca.intelligence.events import EXTRACTION_COMPLETED, ExtractionCompleted
from eca.intelligence.models import extractions_table
from eca.intelligence.output_schemas import Adjudication, EmailExtraction
from eca.intelligence.output_schemas.adjudicate import SCHEMA_VERSION as ADJ_SCHEMA
from eca.intelligence.output_schemas.email_extract import SCHEMA_VERSION as EMAIL_SCHEMA
from eca.intelligence.provider.client import AIClient
from eca.intelligence.provider.types import ProviderError, RoleDisabled, SchemaInvalid
from eca.platform.events import NewEvent
from eca.platform.ids import uuid7
from eca.platform.outbox import publish
from eca.platform.uow import UnitOfWork

PROMPTS_DIR = Path(__file__).parent / "prompts"
EMAIL_PROMPT_VERSION = "email_extract/v1"
ADJ_PROMPT_VERSION = "adjudicate/v1"
ATTEMPT_CAP = 4  # model calls per (source, content_hash, pipeline, prompt_version), incl. repair (§7)
CODE_PATH_SQL = "SELECT set_config('eca.code_path', :path, true)"


@dataclass(frozen=True)
class Participant:
    label: str  # "sender" | "to" | "cc"
    email: str
    name: str | None
    is_self: bool


@dataclass(frozen=True)
class Candidate:
    code: str  # C1..C8
    entity_type: str  # work_item | decision
    entity_id: UUID
    version: int
    summary: str  # rendered text (title, owner, due, status); no IDs
    rejected: bool = False


@dataclass(frozen=True)
class EmailInput:
    source_item_id: UUID
    content_hash: bytes
    occurred_at: datetime.datetime
    user_name: str
    subject: str | None
    body: str
    participants: tuple[Participant, ...]
    candidates: tuple[Candidate, ...] = ()
    negative_examples: tuple[str, ...] = ()


@dataclass(frozen=True)
class Claim:
    extraction_id: UUID
    status: str
    attempts: int
    apply_status: str


@dataclass(frozen=True)
class ExtractionRecord:
    id: UUID
    source_item_id: UUID
    pipeline: str
    prompt_version: str
    model: str | None
    status: str
    apply_status: str
    output: dict[str, Any] | None
    candidate_map: dict[str, Any]
    parent_extraction_id: UUID | None
    created_at: datetime.datetime


@dataclass
class RunOutcome:
    ok: bool
    output: dict[str, Any] | None = None
    model: str | None = None
    calls: int = 0
    error_code: str | None = None
    retryable: bool = False
    call_ids: list[UUID] = field(default_factory=list)


def prompt_text(version: str) -> str:
    role, v = version.split("/")
    return (PROMPTS_DIR / role / f"{v}.md").read_text(encoding="utf-8")


def render_email_prompt(inp: EmailInput) -> tuple[str, str]:
    """(system instruction, user content). Deterministic: no IDs, no clock (cassette keys, §5.5)."""
    system = prompt_text(EMAIL_PROMPT_VERSION).format(user_name=inp.user_name)
    lines = ["PARTICIPANTS:"]
    for p in inp.participants:
        me = " (this is the user)" if p.is_self else ""
        lines.append(f"- {p.label}: {p.name or ''} <{p.email}>{me}")
    lines.append("CANDIDATES:")
    if inp.candidates:
        for c in inp.candidates:
            flag = " [REJECTED by the user]" if c.rejected else ""
            lines.append(f"- {c.code}: {c.summary}{flag}")
    else:
        lines.append("- none")
    if inp.negative_examples:
        lines.append("PREVIOUSLY REJECTED BY THE USER (do not extract similar statements):")
        lines.extend(f"- {x}" for x in inp.negative_examples)
    lines.append(f"DATE: {inp.occurred_at.astimezone(datetime.UTC).isoformat()}")
    lines.append(f"SUBJECT: {inp.subject or ''}")
    lines.append("EMAIL (untrusted data, verbatim):")
    lines.append("<<<EMAIL")
    lines.append(inp.body)
    lines.append("EMAIL>>>")
    return system, "\n".join(lines)


def input_hash(inp: EmailInput) -> bytes:
    system, content = render_email_prompt(inp)
    return hashlib.sha256(json.dumps([EMAIL_PROMPT_VERSION, system, content]).encode("utf-8")).digest()


def candidate_map(inp: EmailInput) -> dict[str, Any]:
    return {
        c.code: {
            "entity_type": c.entity_type,
            "entity_id": str(c.entity_id),
            "version": c.version,
            "rejected": c.rejected,
        }
        for c in inp.candidates
    }


async def set_code_path(uow: UnitOfWork, path: str) -> None:
    """Mark the transaction as an extract or apply code path (RT-12 audit trigger, §6.3)."""
    from sqlalchemy import text

    await uow.session.execute(text(CODE_PATH_SQL), {"path": path})


async def claim(
    uow: UnitOfWork,
    inp: EmailInput,
    *,
    pipeline: str = "email_extract",
    prompt_version: str = EMAIL_PROMPT_VERSION,
    parent_extraction_id: UUID | None = None,
) -> Claim:
    t = extractions_table
    await uow.session.execute(
        insert(t)
        .values(
            id=uuid7(),
            user_id=uow.user_id,
            source_item_id=inp.source_item_id,
            content_hash=inp.content_hash,
            pipeline=pipeline,
            prompt_version=prompt_version,
            schema_version=EMAIL_SCHEMA if pipeline == "email_extract" else ADJ_SCHEMA,
            input_hash=input_hash(inp),
            candidate_map=candidate_map(inp),
            parent_extraction_id=parent_extraction_id,
            status="running",
        )
        .on_conflict_do_nothing()
    )
    row = (
        await uow.session.execute(
            select(t.c.id, t.c.status, t.c.attempts, t.c.apply_status)
            .where(
                t.c.source_item_id == inp.source_item_id,
                t.c.content_hash == inp.content_hash,
                t.c.pipeline == pipeline,
                t.c.prompt_version == prompt_version,
                t.c.parent_extraction_id.is_not_distinct_from(parent_extraction_id),
            )
            .with_for_update()
        )
    ).one()
    return Claim(
        extraction_id=row.id, status=row.status, attempts=row.attempts, apply_status=row.apply_status
    )


async def run_email_extract(
    client: AIClient, inp: EmailInput, *, attempts_used: int, user_id: UUID | None
) -> RunOutcome:
    """AI-01 with the attempt policy (§7): fallback from attempt 3, one repair, cap 4. No DB."""
    system, content = render_email_prompt(inp)
    outcome = RunOutcome(ok=False)
    repair_note: str | None = None
    repaired = False
    attempt = attempts_used
    while attempt < ATTEMPT_CAP:
        attempt += 1
        outcome.calls += 1
        try:
            result = await client.generate(
                "email_extract",
                prompt_version=EMAIL_PROMPT_VERSION,
                schema_version=EMAIL_SCHEMA,
                output_model=EmailExtraction,
                contents=[content],
                system_instruction=system,
                user_id=user_id,
                attempt=attempt,
                use_fallback=attempt >= 3,
                repair_note=repair_note,
            )
        except SchemaInvalid as exc:
            if repaired:
                outcome.error_code = "schema_invalid"
                return outcome
            repaired, repair_note = True, exc.summary
            continue
        except ProviderError as exc:
            outcome.error_code = type(exc).__name__
            outcome.retryable = exc.status.value in {"timeout", "rate_limited", "provider_error"}
            if not outcome.retryable:
                return outcome
            continue
        outcome.ok = True
        outcome.output = result.output.model_dump(mode="json")
        outcome.model = result.model
        if result.call_id is not None:
            outcome.call_ids.append(result.call_id)
        return outcome
    outcome.retryable = False
    outcome.error_code = outcome.error_code or "attempt_cap"
    return outcome


async def run_adjudication(
    client: AIClient, inp: EmailInput, quote: str, *, user_id: UUID | None
) -> RunOutcome:
    """AI-02, disabled until experiment X1: ``RoleDisabled`` is returned as a skipped outcome."""
    system = prompt_text(ADJ_PROMPT_VERSION)
    _, content = render_email_prompt(inp)
    try:
        result = await client.generate(
            "adjudicate",
            prompt_version=ADJ_PROMPT_VERSION,
            schema_version=ADJ_SCHEMA,
            output_model=Adjudication,
            contents=[content, f"STATEMENT TO ADJUDICATE: {quote}"],
            system_instruction=system,
            user_id=user_id,
        )
    except RoleDisabled:
        return RunOutcome(ok=False, error_code="role_disabled")
    except (SchemaInvalid, ProviderError) as exc:
        return RunOutcome(ok=False, error_code=type(exc).__name__, calls=1)
    return RunOutcome(ok=True, output=result.output.model_dump(mode="json"), model=result.model, calls=1)


async def store_success(uow: UnitOfWork, claim_: Claim, outcome: RunOutcome) -> None:
    t = extractions_table
    row = (
        await uow.session.execute(
            update(t)
            .where(t.c.id == claim_.extraction_id, t.c.status.in_(["running", "failed_retryable"]))
            .values(
                status="succeeded",
                output=outcome.output,
                model=outcome.model,
                attempts=t.c.attempts + outcome.calls,
                apply_status="pending",
                error_code=None,
            )
            .returning(t.c.source_item_id, t.c.pipeline)
        )
    ).one_or_none()
    if row is None:
        return  # a concurrent run stored it first
    await ensure_completed_event(uow, claim_.extraction_id, row.source_item_id, row.pipeline)


async def ensure_completed_event(
    uow: UnitOfWork, extraction_id: UUID, source_item_id: UUID, pipeline: str
) -> None:
    await publish(
        uow,
        NewEvent(
            event_type=EXTRACTION_COMPLETED,
            aggregate_type="extraction",
            aggregate_id=extraction_id,
            payload=ExtractionCompleted(
                extraction_id=extraction_id, source_item_id=source_item_id, pipeline=pipeline
            ),
        ),
    )


async def store_failure(uow: UnitOfWork, claim_: Claim, outcome: RunOutcome) -> str:
    """Returns the new status: ``failed_retryable`` or ``failed_permanent`` (cap or non-retryable)."""
    t = extractions_table
    attempts = claim_.attempts + outcome.calls
    status = "failed_retryable" if outcome.retryable and attempts < ATTEMPT_CAP else "failed_permanent"
    await uow.session.execute(
        update(t)
        .where(t.c.id == claim_.extraction_id, t.c.status != "succeeded")
        .values(status=status, attempts=attempts, error_code=outcome.error_code)
    )
    return status


def _record(row: Any) -> ExtractionRecord:
    return ExtractionRecord(
        id=row.id,
        source_item_id=row.source_item_id,
        pipeline=row.pipeline,
        prompt_version=row.prompt_version,
        model=row.model,
        status=row.status,
        apply_status=row.apply_status,
        output=row.output,
        candidate_map=row.candidate_map or {},
        parent_extraction_id=row.parent_extraction_id,
        created_at=row.created_at,
    )


_COLS = (
    extractions_table.c.id,
    extractions_table.c.source_item_id,
    extractions_table.c.pipeline,
    extractions_table.c.prompt_version,
    extractions_table.c.model,
    extractions_table.c.status,
    extractions_table.c.apply_status,
    extractions_table.c.output,
    extractions_table.c.candidate_map,
    extractions_table.c.parent_extraction_id,
    extractions_table.c.created_at,
)


async def get_extraction(
    uow: UnitOfWork, extraction_id: UUID, *, for_update: bool = False
) -> ExtractionRecord:
    stmt = select(*_COLS).where(extractions_table.c.id == extraction_id)
    if for_update:
        stmt = stmt.with_for_update()
    return _record((await uow.session.execute(stmt)).one())


async def latest_pending_for_source(uow: UnitOfWork, source_item_id: UUID) -> ExtractionRecord | None:
    t = extractions_table
    row = (
        await uow.session.execute(
            select(*_COLS)
            .where(
                t.c.source_item_id == source_item_id, t.c.status == "succeeded", t.c.apply_status == "pending"
            )
            .order_by(t.c.created_at.desc())
            .limit(1)
        )
    ).one_or_none()
    return None if row is None else _record(row)


async def succeeded_extractions(uow: UnitOfWork) -> list[ExtractionRecord]:
    """Every succeeded extraction of the user (R2 re-apply), oldest first."""
    t = extractions_table
    rows = await uow.session.execute(
        select(*_COLS).where(t.c.status == "succeeded").order_by(t.c.created_at, t.c.id)
    )
    return [_record(r) for r in rows]


async def mark_applied(uow: UnitOfWork, extraction_id: UUID, *, at: datetime.datetime) -> None:
    t = extractions_table
    await uow.session.execute(
        update(t).where(t.c.id == extraction_id).values(apply_status="applied", applied_at=at)
    )


async def mark_apply_failed(uow: UnitOfWork, extraction_id: UUID) -> None:
    t = extractions_table
    await uow.session.execute(update(t).where(t.c.id == extraction_id).values(apply_status="apply_failed"))


async def reset_apply(uow: UnitOfWork) -> int:
    """R2: every applied extraction back to ``pending`` (no AI call)."""
    t = extractions_table
    result = await uow.session.execute(
        update(t)
        .where(t.c.status == "succeeded", t.c.apply_status == "applied")
        .values(apply_status="pending", applied_at=None)
    )
    return int(result.rowcount)  # type: ignore[attr-defined]
