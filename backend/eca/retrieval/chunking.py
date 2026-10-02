"""Chunking (CONTEXT_ARCHITECTURE.md §9.9, §9.10). Pure functions: same input, same chunks.

Email: one chunk per message (``body_clean``: quotes and signatures already removed), split at
paragraph boundaries above 1,200 tokens; a last piece under 50 tokens joins the previous chunk.
Calendar event: one chunk with the description and the attendee names. Tokens are estimated as
⌈characters / 4⌉ (no tokenizer call).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

MAX_TOKENS = 1200
MIN_TOKENS = 50
CHARS_PER_TOKEN = 4

_PARAGRAPH = re.compile(r"\n\s*\n")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def estimate_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


@dataclass(frozen=True)
class ChunkDraft:
    index: int
    kind: str  # email | calendar_event
    title: str | None
    text: str
    token_count: int


def _pack(pieces: Sequence[str], joiner: str, limit: int) -> list[str]:
    """Greedily join pieces while the result stays within ``limit`` tokens."""
    out: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}{joiner}{piece}" if current else piece
        if current and estimate_tokens(candidate) > limit:
            out.append(current)
            current = piece
        else:
            current = candidate
    if current:
        out.append(current)
    return out


def _split_oversized(paragraph: str) -> list[str]:
    """A paragraph above the limit: split at sentence ends, then at whitespace, then by length."""
    pieces: list[str] = []
    for sentence in _SENTENCE_END.split(paragraph):
        if estimate_tokens(sentence) <= MAX_TOKENS:
            pieces.append(sentence)
            continue
        for word_run in _pack(sentence.split(), " ", MAX_TOKENS):
            if estimate_tokens(word_run) <= MAX_TOKENS:
                pieces.append(word_run)
            else:  # one "word" longer than the limit (an encoded blob): cut by characters
                step = MAX_TOKENS * CHARS_PER_TOKEN
                pieces.extend(word_run[i : i + step] for i in range(0, len(word_run), step))
    return _pack(pieces, " ", MAX_TOKENS)


def split_text(text: str) -> list[str]:
    body = text.strip()
    if not body:
        return []
    if estimate_tokens(body) <= MAX_TOKENS:
        return [body]
    paragraphs: list[str] = []
    for paragraph in _PARAGRAPH.split(body):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if estimate_tokens(paragraph) > MAX_TOKENS:
            paragraphs.extend(_split_oversized(paragraph))
        else:
            paragraphs.append(paragraph)
    parts = _pack(paragraphs, "\n\n", MAX_TOKENS)
    if len(parts) > 1 and estimate_tokens(parts[-1]) < MIN_TOKENS:
        tail = parts.pop()
        parts[-1] = f"{parts[-1]}\n\n{tail}"
    return parts


def email_chunks(subject: str | None, body_clean: str | None) -> list[ChunkDraft]:
    title = (subject or "").strip() or None
    parts = split_text(body_clean or "")
    if not parts and title:
        parts = [title]  # a subject-only message is still findable
    return [ChunkDraft(i, "email", title, part, estimate_tokens(part)) for i, part in enumerate(parts)]


def calendar_chunks(
    title: str | None, description: str | None, attendee_names: Sequence[str]
) -> list[ChunkDraft]:
    lines = [(description or "").strip()]
    names = [n for n in attendee_names if n]
    if names:
        lines.append("Attendees: " + ", ".join(names))
    text = "\n".join(line for line in lines if line) or (title or "").strip()
    if not text:
        return []
    clean_title = (title or "").strip() or None
    return [ChunkDraft(0, "calendar_event", clean_title, text, estimate_tokens(text))]
