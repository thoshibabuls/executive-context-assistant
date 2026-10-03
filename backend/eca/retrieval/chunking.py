"""Chunking (CONTEXT_ARCHITECTURE.md §9.9, §9.10). Pure functions: same input, same chunks.

Email: one chunk per message (``body_clean``: quotes and signatures already removed), split at
paragraph boundaries above 1,200 tokens; a last piece under 50 tokens joins the previous chunk.
Calendar event: one chunk with the description and the attendee names. Transcript (§9.11):
speaker-turn windows of 600-800 tokens with a one-turn overlap. Tokens are estimated as
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
    kind: str  # email | calendar_event | transcript
    title: str | None
    text: str
    token_count: int
    start_ms: int | None = None  # transcript windows: offsets from the recording start
    end_ms: int | None = None


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


# ---------------------------------------------------------------- transcripts (§9.11)

WINDOW_TARGET = 600
WINDOW_MAX = 800
OVERLAP_MAX = 200


@dataclass(frozen=True)
class TranscriptLine:
    start_ms: int | None
    end_ms: int | None
    speaker: str  # the mapped person's display name, else the label
    text: str


@dataclass(frozen=True)
class _Turn:
    start_ms: int | None
    end_ms: int | None
    text: str
    tokens: int


def _stamp(ms: int | None) -> str:
    if ms is None:
        return ""
    seconds = ms // 1000
    return f"[{seconds // 60:02d}:{seconds % 60:02d}] "


def _turns(lines: Sequence[TranscriptLine]) -> list[_Turn]:
    """Consecutive lines of one speaker form a turn; a turn above 800 tokens is split at sentence
    ends."""
    grouped: list[list[TranscriptLine]] = []
    for line in lines:
        if grouped and grouped[-1][-1].speaker == line.speaker:
            grouped[-1].append(line)
        else:
            grouped.append([line])
    turns: list[_Turn] = []
    for group in grouped:
        first, last = group[0], group[-1]
        body = " ".join(x.text.strip() for x in group if x.text.strip())
        prefix = f"{_stamp(first.start_ms)}{first.speaker}: "
        pieces = [body]
        if estimate_tokens(prefix + body) > WINDOW_MAX:
            pieces = _pack(_SENTENCE_END.split(body), " ", WINDOW_MAX - estimate_tokens(prefix))
            pieces = [
                q for p in pieces for q in (_split_oversized(p) if estimate_tokens(p) > WINDOW_MAX else [p])
            ]
        for piece in pieces:
            text = prefix + piece
            turns.append(_Turn(first.start_ms, last.end_ms, text, estimate_tokens(text)))
    return turns


def transcript_chunks(title: str | None, lines: Sequence[TranscriptLine]) -> list[ChunkDraft]:
    """Windows packed greedily from turns: close at 600 tokens or before the next turn would pass
    800; the next window starts with the previous window's last turn when it has at most 200
    tokens; a last window under 50 tokens joins the previous one."""
    turns = _turns(lines)
    windows: list[list[_Turn]] = []
    current: list[_Turn] = []
    size = 0
    for turn in turns:
        if current and (size >= WINDOW_TARGET or size + turn.tokens > WINDOW_MAX):
            windows.append(current)
            tail = current[-1]
            current, size = ([tail], tail.tokens) if tail.tokens <= OVERLAP_MAX else ([], 0)
        current.append(turn)
        size += turn.tokens
    if current and (not windows or current != windows[-1][-1:]):
        windows.append(current)
    if len(windows) > 1 and sum(t.tokens for t in windows[-1]) < MIN_TOKENS:
        tail_window = windows.pop()
        windows[-1] = windows[-1] + [t for t in tail_window if t is not windows[-1][-1]]
    clean_title = (title or "").strip() or None
    out: list[ChunkDraft] = []
    for i, window in enumerate(windows):
        text = "\n".join(t.text for t in window)
        starts = [t.start_ms for t in window if t.start_ms is not None]
        ends = [t.end_ms for t in window if t.end_ms is not None]
        out.append(
            ChunkDraft(
                i,
                "transcript",
                clean_title,
                text,
                estimate_tokens(text),
                min(starts) if starts else None,
                max(ends) if ends else None,
            )
        )
    return out
