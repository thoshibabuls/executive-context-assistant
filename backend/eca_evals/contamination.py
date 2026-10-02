"""Contamination check (AI_EVALUATION.md §3.3 rule 4, gate A0).

Prompts and few-shot files must not contain text from the ``test``, ``sealed`` or ``challenge``
splits. Texts are compared as word n-grams (lower-case, alphanumeric words); any shared n-gram
fails. N-grams that also occur in ``dev`` texts are allowed: ``world_v1`` is generated from
templates, so boilerplate shared by every split is not case content, and ``dev`` is the split
prompts may draw from (§3.2).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_N = 8
SCANNED_SUFFIXES = {".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".py"}
_WORD = re.compile(r"[a-z0-9]+")


def ngrams(text: str, n: int = DEFAULT_N) -> set[tuple[str, ...]]:
    words = _WORD.findall(text.lower())
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


@dataclass(frozen=True)
class Hit:
    file: str
    case_id: str
    ngram: str


def scan(
    protected: Mapping[str, str],
    files: Iterable[Path],
    *,
    n: int = DEFAULT_N,
    root: Path | None = None,
    allowed: Iterable[str] = (),
) -> list[Hit]:
    """Shared n-grams between protected case texts and candidate prompt/few-shot files."""
    allowed_grams: set[tuple[str, ...]] = set()
    for text in allowed:
        allowed_grams |= ngrams(text, n)
    index: dict[tuple[str, ...], str] = {}
    for case_id, text in sorted(protected.items()):
        for gram in ngrams(text, n) - allowed_grams:
            index.setdefault(gram, case_id)
    hits: list[Hit] = []
    for path in sorted(files):
        if path.suffix not in SCANNED_SUFFIXES or not path.is_file():
            continue
        shown = path.relative_to(root).as_posix() if root else path.as_posix()
        for gram in sorted(ngrams(path.read_text(encoding="utf-8", errors="replace"), n)):
            if gram in index:
                hits.append(Hit(shown, index[gram], " ".join(gram)))
    return hits


def candidate_files(directories: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    for directory in directories:
        if directory.is_dir():
            out.extend(p for p in directory.rglob("*") if p.is_file())
    return sorted(out)
