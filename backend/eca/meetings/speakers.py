"""Speaker mapping (slice 4.3, TECHNICAL_DESIGN.md §16.1 "Speaker mapping", BACKEND_DESIGN.md §17.7).

A mapping links a diarization label of a meeting's transcript to a person through
``meeting_participants.speaker_labels``. ``applied`` mappings resolve speakers; ``proposed`` ones
wait for the user. Rules:

- Deterministic first (pure functions here): a self-introduction in a label's first three
  segments matched to exactly one attendee → 0.95 (applied); matched only outside the attendees →
  0.8 (proposed); elimination → 0.9 (applied).
- AI-10 proposals apply only at confidence ≥ 0.9; below that they are stored as ``proposed``.
- User mappings (authority 5, ``mapping_origin = user``) are never replaced by deterministic or AI
  mappings. A label belongs to at most one participant row of a meeting (meeting row lock).
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import any_, delete, select, update
from sqlalchemy.dialects.postgresql import insert

from eca.meetings.events import SPEAKER_MAPPING_CHANGED, SpeakerMappingChanged
from eca.meetings.models import meeting_participants_table, meetings_table, recordings_table
from eca.meetings.transcripts import Segment, load_segments
from eca.platform.errors import Conflict, NotFound, ValidationFailed
from eca.platform.events import NewEvent
from eca.platform.outbox import publish
from eca.platform.uow import UnitOfWork

AUTO_APPLY = 0.9
INTRO_ATTENDEE = 0.95
INTRO_OUTSIDE = 0.8
ELIMINATION = 0.9
INTRO_SEGMENTS = 3
# The lead phrases match in any case ("This is", "my name is"); the name itself must be capitalized.
_NAME = r"([A-Z][\w'\-]+(?:\s+[A-Z][\w'\-]+)?)"
_INTRO = [
    re.compile(rf"\b(?i:I'm|I am|this is|my name is|it's)\s+{_NAME}"),
    re.compile(rf"^(?i:hi|hello|hey)?[,\s]*{_NAME}\s+(?i:here)\b"),
]
_NOT_NAMES = frozenset(
    {
        "here", "going", "sorry", "not", "just", "the", "a", "so", "on", "in", "back", "sure",
        "everyone", "everybody", "all", "we", "we're", "i", "i'm", "it", "it's", "is", "you", "they",
    }
)  # fmt: skip


@dataclass(frozen=True)
class Attendee:
    person_id: UUID
    names: tuple[str, ...]  # normalized (lower-case) display name and name aliases
    is_self: bool


@dataclass(frozen=True)
class Proposal:
    label: str
    person_id: UUID
    confidence: float
    method: str  # self_introduction | elimination | ai_proposal
    origin: str  # deterministic | ai


@dataclass(frozen=True)
class SpeakerMapping:
    person_id: UUID
    labels: tuple[str, ...]
    status: str | None  # applied | proposed | None (an attendee without a mapping)
    origin: str | None  # deterministic | ai | user
    method: str | None
    confidence: float | None
    model: str | None
    extraction_id: UUID | None
    derived_at: datetime.datetime | None
    confirmed_at: datetime.datetime | None
    participant_origin: str  # source | ai | user


# ---------------------------------------------------------------- pure matching


def introduced_names(texts: Sequence[str]) -> list[str]:
    """Names a speaker gives for themself ("I'm Priya", "this is Priya Raman", "Priya here")."""
    found: list[str] = []
    for text in texts:
        for pattern in _INTRO:
            for match in pattern.finditer(text):
                name = match.group(1).strip()
                if name.split()[0].lower() not in _NOT_NAMES:
                    found.append(name)
    return found


def match_attendees(name: str, attendees: Sequence[Attendee]) -> list[UUID]:
    """Full-name match first, then a unique first-name match (§13.7 rules)."""
    norm = " ".join(name.lower().split())
    full = [a.person_id for a in attendees if norm in a.names]
    if full:
        return sorted(set(full))
    first = norm.split()[0]
    return sorted({a.person_id for a in attendees if any(n.split()[0] == first for n in a.names if n)})


def first_segments(segments: Sequence[Segment]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for s in segments:
        if s.speaker_label is None:
            continue
        texts = out.setdefault(s.speaker_label, [])
        if len(texts) < INTRO_SEGMENTS:
            texts.append(s.text)
    return out


def introduction_proposals(segments: Sequence[Segment], attendees: Sequence[Attendee]) -> list[Proposal]:
    """Self-introductions matched to exactly one attendee (0.95). Unmatched names are returned by
    :func:`unmatched_introductions` for a lookup among all persons."""
    out: list[Proposal] = []
    for label, texts in sorted(first_segments(segments).items()):
        for name in introduced_names(texts):
            matches = match_attendees(name, attendees)
            if len(matches) == 1:
                out.append(Proposal(label, matches[0], INTRO_ATTENDEE, "self_introduction", "deterministic"))
                break
    return out


def unmatched_introductions(segments: Sequence[Segment], attendees: Sequence[Attendee]) -> dict[str, str]:
    out: dict[str, str] = {}
    for label, texts in sorted(first_segments(segments).items()):
        names = introduced_names(texts)
        if names and not any(len(match_attendees(n, attendees)) == 1 for n in names):
            out[label] = names[0]
    return out


def elimination(
    labels: Sequence[str], applied: dict[str, UUID], attendees: Sequence[Attendee], self_id: UUID
) -> Proposal | None:
    """Every label but one mapped, exactly one attendee other than the user unmapped, and the user
    mapped or absent → the last label is that attendee (0.9)."""
    unmapped_labels = [label for label in labels if label not in applied]
    if len(unmapped_labels) != 1:
        return None
    mapped_people = set(applied.values())
    others = [a.person_id for a in attendees if not a.is_self and a.person_id not in mapped_people]
    self_attends = any(a.is_self for a in attendees)
    if len(others) != 1 or (self_attends and self_id not in mapped_people):
        return None
    return Proposal(unmapped_labels[0], others[0], ELIMINATION, "elimination", "deterministic")


# ---------------------------------------------------------------- reads


def _mapping(r: Any) -> SpeakerMapping:
    return SpeakerMapping(
        person_id=r.person_id,
        labels=tuple(r.speaker_labels or ()),
        status=r.mapping_status,
        origin=r.mapping_origin,
        method=r.mapping_method,
        confidence=float(r.mapping_confidence) if r.mapping_confidence is not None else None,
        model=r.mapping_model,
        extraction_id=r.mapping_extraction_id,
        derived_at=r.mapping_derived_at,
        confirmed_at=r.mapping_confirmed_at,
        participant_origin=r.origin,
    )


async def speaker_mappings(uow: UnitOfWork, meeting_id: UUID) -> list[SpeakerMapping]:
    mp = meeting_participants_table
    rows = await uow.session.execute(
        select(mp).where(mp.c.user_id == uow.user_id, mp.c.meeting_id == meeting_id).order_by(mp.c.person_id)
    )
    return [_mapping(r) for r in rows]


async def applied_labels(uow: UnitOfWork, meeting_id: UUID) -> dict[str, UUID]:
    """Label → person for ``applied`` mappings (the only ones that resolve a speaker)."""
    return {
        label: m.person_id
        for m in await speaker_mappings(uow, meeting_id)
        if m.status == "applied"
        for label in m.labels
    }


async def transcript_labels(uow: UnitOfWork, meeting_id: UUID) -> list[str]:
    """Distinct labels of the current transcript of the meeting's recording, in first-use order."""
    r = recordings_table
    row = (
        await uow.session.execute(
            select(r.c.id, r.c.transcription_model).where(
                r.c.user_id == uow.user_id, r.c.meeting_id == meeting_id
            )
        )
    ).one_or_none()
    if row is None or row.transcription_model is None:
        return []
    seen: list[str] = []
    for s in await load_segments(uow, row.id, row.transcription_model):
        if s.speaker_label and s.speaker_label not in seen:
            seen.append(s.speaker_label)
    return seen


# ---------------------------------------------------------------- writes


async def _lock_meeting(uow: UnitOfWork, meeting_id: UUID) -> Any:
    m = meetings_table
    row = (
        await uow.session.execute(
            select(m.c.id, m.c.version)
            .where(m.c.user_id == uow.user_id, m.c.id == meeting_id, m.c.deleted_at.is_(None))
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise NotFound("meeting not found")
    return row


async def _remove_label(uow: UnitOfWork, meeting_id: UUID, label: str) -> None:
    """Remove a label from every row; rows that existed only for a mapping go when empty."""
    mp = meeting_participants_table
    for r in await uow.session.execute(
        select(mp.c.person_id, mp.c.speaker_labels, mp.c.origin).where(
            mp.c.user_id == uow.user_id, mp.c.meeting_id == meeting_id, any_(mp.c.speaker_labels) == label
        )
    ):
        remaining = [x for x in (r.speaker_labels or []) if x != label]
        if not remaining and r.origin != "source":
            await uow.session.execute(
                delete(mp).where(mp.c.meeting_id == meeting_id, mp.c.person_id == r.person_id)
            )
            continue
        values: dict[str, Any] = {"speaker_labels": remaining}
        if not remaining:
            values.update(
                mapping_status=None,
                mapping_origin=None,
                mapping_method=None,
                mapping_confidence=None,
                mapping_extraction_id=None,
                mapping_model=None,
                mapping_derived_at=None,
                mapping_confirmed_at=None,
            )
        await uow.session.execute(
            update(mp).where(mp.c.meeting_id == meeting_id, mp.c.person_id == r.person_id).values(**values)
        )


async def _assign(
    uow: UnitOfWork,
    meeting_id: UUID,
    person_id: UUID,
    label: str,
    *,
    status: str,
    origin: str,
    method: str,
    confidence: float,
    extraction_id: UUID | None,
    model: str | None,
    now: datetime.datetime,
) -> None:
    mp = meeting_participants_table
    current = (
        await uow.session.execute(
            select(mp.c.speaker_labels, mp.c.mapping_status).where(
                mp.c.meeting_id == meeting_id, mp.c.person_id == person_id
            )
        )
    ).one_or_none()
    labels = list(current.speaker_labels or []) if current is not None else []
    if current is not None and current.mapping_status == "proposed" and status == "applied":
        labels = []  # an applied mapping replaces this person's open proposals
    if label not in labels:
        labels.append(label)
    mapping = {
        "speaker_labels": labels,
        "mapping_status": status,
        "mapping_origin": origin,
        "mapping_method": method,
        "mapping_confidence": confidence,
        "mapping_extraction_id": extraction_id,
        "mapping_model": model,
        "mapping_derived_at": now,
        "mapping_confirmed_at": now if origin == "user" else None,
    }
    # A participant row that exists only because of a mapping is inferred (A13): origin ai or user.
    await uow.session.execute(
        insert(mp)
        .values(
            meeting_id=meeting_id,
            person_id=person_id,
            user_id=uow.user_id,
            is_organizer=False,
            origin="user" if origin == "user" else "ai",
            **mapping,
        )
        .on_conflict_do_update(index_elements=["meeting_id", "person_id"], set_=mapping)
    )


async def apply_proposals(
    uow: UnitOfWork,
    meeting_id: UUID,
    proposals: Sequence[Proposal],
    *,
    extraction_id: UUID | None = None,
    model: str | None = None,
    now: datetime.datetime,
) -> list[str]:
    """Deterministic and AI proposals under the rules of the module docstring. Returns the labels
    whose applied mapping changed."""
    await _lock_meeting(uow, meeting_id)
    changed: list[str] = []
    for p in proposals:
        current = await speaker_mappings(uow, meeting_id)
        label_owner = next((m for m in current if p.label in m.labels), None)
        if label_owner is not None and (label_owner.status == "applied" or label_owner.origin == "user"):
            continue  # an applied or user mapping is never replaced
        person = next((m for m in current if m.person_id == p.person_id), None)
        if person is not None and person.origin == "user":
            continue
        applied = p.confidence >= AUTO_APPLY
        if not applied and person is not None and person.status == "applied":
            continue  # a proposal for a person who already has an applied mapping is not stored
        if label_owner is not None:
            await _remove_label(uow, meeting_id, p.label)
        await _assign(
            uow,
            meeting_id,
            p.person_id,
            p.label,
            status="applied" if applied else "proposed",
            origin=p.origin,
            method=p.method,
            confidence=p.confidence,
            extraction_id=extraction_id if p.origin == "ai" else None,
            model=model if p.origin == "ai" else None,
            now=now,
        )
        if applied:
            changed.append(p.label)
    return changed


async def set_speaker_mapping(
    uow: UnitOfWork,
    meeting_id: UUID,
    mappings: Sequence[tuple[str, UUID | None]],
    *,
    base_version: int | None,
    known_persons: set[UUID],
    now: datetime.datetime,
) -> tuple[dict[str, UUID], dict[str, UUID]]:
    """The user's mapping (authority 5). ``person_id`` None removes the label from every row
    ("none of these"). Returns the applied label → person maps before and after. The caller
    re-points items (``work.remap_speaker_items``) in the same transaction."""
    row = await _lock_meeting(uow, meeting_id)
    if base_version is not None and base_version != row.version:
        raise Conflict("the meeting changed; reload it", details={"reason": "version_mismatch"})
    labels = set(await transcript_labels(uow, meeting_id))
    for label, person_id in mappings:
        if label not in labels:
            raise ValidationFailed(f"unknown speaker label {label!r}")
        if person_id is not None and person_id not in known_persons:
            raise NotFound("person not found")
    before = await applied_labels(uow, meeting_id)
    for label, person_id in mappings:
        await _remove_label(uow, meeting_id, label)
        if person_id is not None:
            await _assign(
                uow,
                meeting_id,
                person_id,
                label,
                status="applied",
                origin="user",
                method="user",
                confidence=1.0,
                extraction_id=None,
                model=None,
                now=now,
            )
    m = meetings_table
    version = (
        await uow.session.execute(
            update(m).where(m.c.id == meeting_id).values(version=m.c.version + 1).returning(m.c.version)
        )
    ).scalar_one()
    after = await applied_labels(uow, meeting_id)
    await publish_mapping_changed(uow, meeting_id, sorted(label for label, _ in mappings), version)
    return before, after


async def publish_mapping_changed(uow: UnitOfWork, meeting_id: UUID, labels: list[str], version: int) -> None:
    r = recordings_table
    recording_id = (
        await uow.session.execute(
            select(r.c.id).where(r.c.user_id == uow.user_id, r.c.meeting_id == meeting_id)
        )
    ).scalar_one_or_none()
    await publish(
        uow,
        NewEvent(
            SPEAKER_MAPPING_CHANGED,
            "meeting",
            meeting_id,
            SpeakerMappingChanged(
                meeting_id=meeting_id, recording_id=recording_id, labels=labels, version=version
            ),
        ),
    )
