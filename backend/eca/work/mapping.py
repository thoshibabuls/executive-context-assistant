"""Deterministic statement-to-direction mapping (AI_PIPELINE.md §5.5, TECHNICAL_DESIGN.md §8.3).

The model labels ``statement_kind`` and refs; code decides type, direction, owner and maximum
strength from speaker, owner and the user's self Person. The mapping is the same for email and
transcripts; a speaker that is not mapped gives direction ``unresolved``.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class Mapped:
    type: str
    direction: str
    owner_id: UUID | None
    requester_id: UUID | None
    counterparty_id: UUID | None
    strength: str  # explicit | probable
    owner_inconsistent: bool = False  # the model's owner_ref disagrees with the mapping


def map_statement(
    *,
    kind: str,
    speaker_id: UUID | None,
    self_id: UUID,
    owner_id: UUID | None,
    beneficiary_id: UUID | None,
    addressees: tuple[UUID, ...],
    forwarded: bool = False,
) -> Mapped:
    if speaker_id is None:
        return Mapped(_type(kind), "unresolved", owner_id, None, None, "probable")
    speaker_is_self = speaker_id == self_id
    owner: UUID | None
    if kind == "promise":
        owner = speaker_id
        inconsistent = owner_id is not None and owner_id != speaker_id
        if speaker_is_self and not forwarded:
            return Mapped(
                "commitment",
                "my_commitment",
                owner,
                None,
                beneficiary_id,
                _cap("explicit", forwarded),
                inconsistent,
            )
        to_self = beneficiary_id == self_id or self_id in addressees
        direction = "waiting_for" if to_self else "observed"
        if forwarded and speaker_is_self:
            direction = "observed"
        return Mapped(
            "commitment",
            direction,
            owner,
            None,
            beneficiary_id or (self_id if to_self else None),
            _cap("explicit", forwarded),
            inconsistent,
        )
    if kind == "request":
        if speaker_is_self:
            owner = owner_id if owner_id != self_id else None
            if owner is None and len(addressees) == 1:
                owner = addressees[0]
            return Mapped("request", "delegated", owner, self_id, owner, _cap("explicit", forwarded))
        owner = owner_id if owner_id is not None else (self_id if self_id in addressees else None)
        if owner == self_id:
            return Mapped("request", "my_task", self_id, speaker_id, speaker_id, _cap("explicit", forwarded))
        return Mapped("request", "observed", owner, speaker_id, speaker_id, _cap("explicit", forwarded))
    if kind == "acceptance":
        owner = speaker_id
        direction = "my_commitment" if speaker_is_self else "waiting_for"
        return Mapped("commitment", direction, owner, None, beneficiary_id, _cap("explicit", forwarded))
    if kind == "report_commitment":
        owner = owner_id if owner_id != speaker_id else None
        to_self = speaker_is_self or beneficiary_id == self_id or self_id in addressees
        if owner == self_id:  # "Avery will send it" said by someone else about the user
            return Mapped("commitment", "my_commitment", owner, None, speaker_id, "probable")
        return Mapped(
            "commitment",
            "waiting_for" if to_self else "observed",
            owner,
            None,
            self_id if to_self else beneficiary_id,
            "probable",
        )
    if kind == "report_request":
        if speaker_is_self:
            requester = owner_id if owner_id not in (None, self_id) else beneficiary_id
            return Mapped("request", "my_task", self_id, requester, requester, "probable")
        return Mapped("request", "observed", speaker_id, owner_id, owner_id, "probable")
    if kind == "assignment":
        target = owner_id
        if target == self_id:
            return Mapped("task", "my_task", self_id, speaker_id, speaker_id, "probable")
        if speaker_is_self:
            return Mapped("task", "delegated", target, self_id, target, "probable")
        return Mapped("task", "observed", target, speaker_id, speaker_id, "probable")
    raise ValueError(f"unknown statement kind {kind!r}")


def _type(kind: str) -> str:
    return {
        "promise": "commitment",
        "acceptance": "commitment",
        "report_commitment": "commitment",
        "request": "request",
        "report_request": "request",
        "assignment": "task",
    }[kind]


def _cap(strength: str, forwarded: bool) -> str:
    return "probable" if forwarded else strength


def authority(*, speaker_id: UUID | None, owner_id: UUID | None, strength: str) -> int:
    """CONTEXT_ARCHITECTURE.md §8.2: 4 owner explicit, 3 other participant explicit, 2 inferred."""
    if strength != "explicit" or speaker_id is None:
        return 2
    return 4 if speaker_id == owner_id else 3
