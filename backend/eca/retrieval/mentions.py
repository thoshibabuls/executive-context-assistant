"""Alias scan for ``entity_mentions`` (CONTEXT_ARCHITECTURE.md §5.2 A4, §12.7). Pure function.

Aliases are matched as whole-word, case-insensitive phrases (any run of whitespace between
words). An alias shared by two entities is ambiguous and never produces a mention, so similar
names are not merged by the scan (CC-42).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class AliasTarget:
    entity_type: str  # person | project
    entity_id: UUID
    alias: str  # normalized: lower case, single spaces
    confidence: float


@dataclass(frozen=True)
class AliasHit:
    entity_type: str
    entity_id: UUID
    surface_text: str
    confidence: float


class AliasMatcher:
    def __init__(self, targets: Sequence[AliasTarget]) -> None:
        by_alias: dict[str, set[tuple[str, UUID]]] = {}
        confidence: dict[tuple[str, UUID], float] = {}
        for t in targets:
            key = " ".join(t.alias.lower().split())
            if key:
                by_alias.setdefault(key, set()).add((t.entity_type, t.entity_id))
                confidence[(t.entity_type, t.entity_id)] = t.confidence
        self._targets = {a: next(iter(e)) for a, e in by_alias.items() if len(e) == 1}
        self._confidence = confidence
        aliases = sorted(self._targets, key=lambda a: (-len(a), a))
        if aliases:
            body = "|".join(r"\s+".join(re.escape(w) for w in a.split(" ")) for a in aliases)
            self._pattern: re.Pattern[str] | None = re.compile(rf"(?<!\w)(?:{body})(?!\w)", re.IGNORECASE)
        else:
            self._pattern = None

    def scan(self, text: str) -> list[AliasHit]:
        """One hit per entity (first surface form), in order of first appearance."""
        if self._pattern is None or not text:
            return []
        seen: set[tuple[str, UUID]] = set()
        hits: list[AliasHit] = []
        for match in self._pattern.finditer(text):
            surface = match.group(0)
            target = self._targets.get(" ".join(surface.lower().split()))
            if target is None or target in seen:
                continue
            seen.add(target)
            hits.append(AliasHit(target[0], target[1], " ".join(surface.split()), self._confidence[target]))
        return hits
