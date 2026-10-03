"""Transcripts: deterministic file parsers, AI-09 segment validation, versioned storage (slice 4.2).

Parsers (TECHNICAL_DESIGN.md §16.1, "Transcript files") are pure functions over bytes: WebVTT, SRT,
TXT and DOCX. No model call. DOCX is a ZIP archive: only ``word/document.xml`` is read, with limits
on the member size, the compression ratio and the paragraph count against zip bombs.

Storage (BACKEND_DESIGN.md §17.7): a version is keyed by ``(recording_id, transcription_model)``
and written in one transaction (delete that key's segments, insert the new ones, set
``recordings.transcription_model`` and ``transcript_hash``). Other versions stay for their evidence.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import itertools
import json
import re
import zipfile
from dataclasses import dataclass
from typing import Any
from uuid import UUID
from xml.etree import ElementTree

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from eca.meetings.models import (
    meeting_participants_table,
    meetings_table,
    recordings_table,
    transcript_segments_table,
)
from eca.platform.ids import uuid7
from eca.platform.uow import UnitOfWork

MAX_SEGMENTS = 20_000
MAX_SEGMENT_CHARS = 4_000
MAX_LABEL_CHARS = 60
DOCX_MAX_XML_BYTES = 20 * 1024 * 1024
DOCX_MAX_RATIO = 100
DOCX_MAX_PARAGRAPHS = 50_000
TIMESTAMP_SLACK_MS = 2_000
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

_CUE_TIME = re.compile(
    r"^\s*((?:\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*((?:\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3})"
)
_VOICE = re.compile(r"^<v(?:\.[^ >]+)?\s+([^>]+)>(.*)$", re.DOTALL)
_TAG = re.compile(r"</?[^>]+>")
_NAME_PREFIX = re.compile(r"^([A-Z][\w.'\- ]{0,40}?):\s+(.+)$", re.DOTALL)
_STAMP = re.compile(r"^\[?((?:\d{1,2}:)?\d{1,2}:\d{2})\]?\s*(.*)$", re.DOTALL)


class TranscriptParseError(ValueError):
    """The file is not a readable transcript (the recording is ``rejected`` as ``unreadable``)."""


@dataclass(frozen=True)
class Segment:
    seq: int
    start_ms: int | None
    end_ms: int | None
    speaker_label: str | None
    text: str


# ---------------------------------------------------------------- parsing helpers


def _ms(stamp: str) -> int:
    parts = stamp.replace(",", ".").split(":")
    seconds = float(parts[-1])
    minutes = int(parts[-2]) if len(parts) >= 2 else 0
    hours = int(parts[-3]) if len(parts) >= 3 else 0
    return round((hours * 3600 + minutes * 60 + seconds) * 1000)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" not in text:
            return text.replace("\r\n", "\n").replace("\r", "\n")
    raise TranscriptParseError("not a UTF-8 or UTF-16 text file")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _TAG.sub("", text)).strip()


def _split_name(text: str) -> tuple[str | None, str]:
    match = _NAME_PREFIX.match(text)
    if match is None:
        return None, text
    return match.group(1).strip()[:MAX_LABEL_CHARS], match.group(2).strip()


def _finish(raw: list[tuple[int | None, int | None, str | None, str]]) -> list[Segment]:
    out: list[Segment] = []
    for start, end, label, text in raw:
        body = text.strip()[:MAX_SEGMENT_CHARS]
        if not body:
            continue
        if start is not None and end is not None and end < start:
            end = start
        out.append(Segment(len(out), start, end, label or None, body))
        if len(out) > MAX_SEGMENTS:
            raise TranscriptParseError("too many segments")
    if not out:
        raise TranscriptParseError("no transcript text")
    return out


def _cues(text: str) -> list[tuple[int, int, list[str]]]:
    """Timed cue blocks of a WebVTT or SRT file: (start, end, text lines)."""
    blocks: list[tuple[int, int, list[str]]] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [x for x in block.split("\n") if x.strip()]
        for i, line in enumerate(lines):
            match = _CUE_TIME.match(line)
            if match is not None:
                blocks.append((_ms(match.group(1)), _ms(match.group(2)), lines[i + 1 :]))
                break
    return blocks


# ---------------------------------------------------------------- parsers


def parse_vtt(data: bytes) -> list[Segment]:
    text = _decode(data)
    if not text.lstrip().startswith("WEBVTT"):
        raise TranscriptParseError("missing WEBVTT header")
    raw: list[tuple[int | None, int | None, str | None, str]] = []
    for start, end, lines in _cues(text):
        joined = " ".join(lines).strip()
        voice = _VOICE.match(joined)
        if voice is not None:
            raw.append((start, end, voice.group(1).strip()[:MAX_LABEL_CHARS], _clean(voice.group(2))))
            continue
        label, body = _split_name(_clean(joined))
        raw.append((start, end, label, body))
    return _finish(raw)


def parse_srt(data: bytes) -> list[Segment]:
    raw: list[tuple[int | None, int | None, str | None, str]] = []
    for start, end, lines in _cues(_decode(data)):
        label, body = _split_name(_clean(" ".join(lines)))
        raw.append((start, end, label, body))
    return _finish(raw)


def _paragraph_segments(paragraphs: list[str]) -> list[Segment]:
    """TXT rules: one segment per non-empty paragraph; optional ``[hh:mm:ss]`` stamp, ``Name:``."""
    raw: list[tuple[int | None, int | None, str | None, str]] = []
    for paragraph in paragraphs:
        text = _clean(paragraph)
        if not text:
            continue
        start: int | None = None
        stamp = _STAMP.match(text)
        if stamp is not None and stamp.group(2):
            start, text = _ms(stamp.group(1)), stamp.group(2).strip()
        label, body = _split_name(text)
        raw.append((start, None, label, body))
    # A stamped paragraph ends where the next stamped one starts.
    stamped = [i for i, r in enumerate(raw) if r[0] is not None]
    for a, b in itertools.pairwise(stamped):
        s, _, label, body = raw[a]
        raw[a] = (s, raw[b][0], label, body)
    if stamped:
        last = stamped[-1]
        s, _, label, body = raw[last]
        raw[last] = (s, s, label, body)
    return _finish(raw)


def parse_txt(data: bytes) -> list[Segment]:
    text = _decode(data)
    paragraphs = re.split(r"\n\s*\n", text)
    if len(paragraphs) == 1:  # one line per utterance is common in exported text
        paragraphs = text.split("\n")
    return _paragraph_segments(paragraphs)


def docx_paragraphs(data: bytes) -> list[str]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise TranscriptParseError("not a DOCX file") from exc
    with archive:
        try:
            info = archive.getinfo("word/document.xml")
        except KeyError as exc:
            raise TranscriptParseError("DOCX without word/document.xml") from exc
        if info.file_size > DOCX_MAX_XML_BYTES:
            raise TranscriptParseError("DOCX document too large")
        if info.compress_size and info.file_size / info.compress_size > DOCX_MAX_RATIO:
            raise TranscriptParseError("DOCX compression ratio too high")
        with archive.open(info) as handle:
            xml = handle.read(DOCX_MAX_XML_BYTES + 1)
    if len(xml) > DOCX_MAX_XML_BYTES:
        raise TranscriptParseError("DOCX document too large")
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise TranscriptParseError("DOCX with a document type declaration")
    try:
        root = ElementTree.fromstring(xml)  # noqa: S314 - DTDs refused above; no external entities
    except ElementTree.ParseError as exc:
        raise TranscriptParseError("DOCX XML is not well formed") from exc
    paragraphs: list[str] = []
    for p in root.iter(f"{_W}p"):
        parts = [t.text or "" for t in p.iter(f"{_W}t")]
        paragraphs.append("".join(parts))
        if len(paragraphs) > DOCX_MAX_PARAGRAPHS:
            raise TranscriptParseError("DOCX has too many paragraphs")
    return paragraphs


def parse_docx(data: bytes) -> list[Segment]:
    return _paragraph_segments(docx_paragraphs(data))


PARSERS = {
    "text/vtt": ("file:vtt", parse_vtt),
    "application/x-subrip": ("file:srt", parse_srt),
    "text/plain": ("file:txt", parse_txt),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ("file:docx", parse_docx),
}


def parse_transcript_file(mime: str, data: bytes) -> tuple[str, list[Segment]]:
    """``(transcription_model, segments)`` for an accepted transcript type."""
    try:
        model, parser = PARSERS[mime]
    except KeyError:
        raise TranscriptParseError("unsupported transcript type") from None
    return model, parser(data)


# ---------------------------------------------------------------- AI-09 output


def validate_model_segments(
    segments: list[tuple[int, int, str, str]], *, duration_ms: int, offset_ms: int = 0, label_prefix: str = ""
) -> list[tuple[int, int, str, str]]:
    """AI_PIPELINE.md §5.10 validation: non-negative, ``end ≥ start``, within the duration + 2 s,
    empty texts dropped, sorted by start. ``offset_ms``/``label_prefix`` place a 60-minute window."""
    limit = duration_ms + TIMESTAMP_SLACK_MS
    out: list[tuple[int, int, str, str]] = []
    for start, end, speaker, text in segments:
        body = text.strip()
        if not body:
            continue
        s, e = start + offset_ms, max(end, start) + offset_ms
        if s < 0 or s > limit:
            continue
        out.append((s, min(e, limit), f"{label_prefix}{speaker.strip()}"[:MAX_LABEL_CHARS], body))
    out.sort(key=lambda x: (x[0], x[1]))
    return out


def number(rows: list[tuple[int | None, int | None, str | None, str]]) -> list[Segment]:
    return [Segment(i, s, e, label, text) for i, (s, e, label, text) in enumerate(rows)]


def transcript_hash(segments: list[Segment]) -> bytes:
    """SHA-256 over the canonical JSON of ``[seq, start_ms, end_ms, speaker_label, text]`` rows."""
    rows = [[s.seq, s.start_ms, s.end_ms, s.speaker_label, s.text] for s in segments]
    canonical = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def duration_of(segments: list[Segment]) -> float | None:
    ends = [s.end_ms if s.end_ms is not None else s.start_ms for s in segments]
    known = [e for e in ends if e is not None]
    return max(known) / 1000 if known else None


# ---------------------------------------------------------------- storage


async def replace_version(
    uow: UnitOfWork, *, recording_id: UUID, transcription_model: str, segments: list[Segment]
) -> None:
    """Delete this version's segments and insert the new ones (the caller's transaction)."""
    t = transcript_segments_table
    await uow.session.execute(
        delete(t).where(
            t.c.user_id == uow.user_id,
            t.c.recording_id == recording_id,
            t.c.transcription_model == transcription_model,
        )
    )
    rows: list[dict[str, Any]] = [
        {
            "id": uuid7(),
            "user_id": uow.user_id,
            "recording_id": recording_id,
            "transcription_model": transcription_model,
            "seq": s.seq,
            "start_ms": s.start_ms,
            "end_ms": s.end_ms,
            "speaker_label": s.speaker_label,
            "text": s.text,
        }
        for s in segments
    ]
    for i in range(0, len(rows), 1000):
        await uow.session.execute(insert(t), rows[i : i + 1000])


async def load_segments(uow: UnitOfWork, recording_id: UUID, transcription_model: str) -> list[Segment]:
    t = transcript_segments_table
    rows = await uow.session.execute(
        select(t.c.seq, t.c.start_ms, t.c.end_ms, t.c.speaker_label, t.c.text)
        .where(
            t.c.user_id == uow.user_id,
            t.c.recording_id == recording_id,
            t.c.transcription_model == transcription_model,
        )
        .order_by(t.c.seq)
    )
    return [Segment(r.seq, r.start_ms, r.end_ms, r.speaker_label, r.text) for r in rows]


async def delete_user_segments(uow: UnitOfWork) -> None:
    t = transcript_segments_table
    await uow.session.execute(delete(t).where(t.c.user_id == uow.user_id))


async def delete_recording_segments(uow: UnitOfWork, recording_ids: list[UUID]) -> None:
    if recording_ids:
        t = transcript_segments_table
        await uow.session.execute(delete(t).where(t.c.recording_id.in_(recording_ids)))


@dataclass(frozen=True)
class TranscriptView:
    """The current transcript version of a recording with what indexing and extraction read."""

    recording_id: UUID
    source_item_id: UUID
    meeting_id: UUID | None
    title: str | None
    starts_at: datetime.datetime  # the meeting start, else the declared start, else the upload time
    transcription_model: str
    transcript_hash: bytes
    duration_s: float | None
    segments: tuple[Segment, ...]
    attendee_ids: tuple[UUID, ...]
    speaker_people: dict[str, UUID]  # label -> person with an applied mapping


async def current_transcript(uow: UnitOfWork, recording_id: UUID) -> TranscriptView | None:
    """None while the recording has no transcript version."""
    r, m, mp = recordings_table, meetings_table, meeting_participants_table
    row = (
        await uow.session.execute(
            select(
                r.c.id,
                r.c.source_item_id,
                r.c.meeting_id,
                r.c.title,
                r.c.occurred_at,
                r.c.created_at,
                r.c.transcription_model,
                r.c.transcript_hash,
                r.c.duration_s,
                m.c.title.label("meeting_title"),
                m.c.starts_at,
            )
            .select_from(r.outerjoin(m, m.c.id == r.c.meeting_id))
            .where(r.c.user_id == uow.user_id, r.c.id == recording_id)
        )
    ).one_or_none()
    if row is None or row.transcription_model is None or row.source_item_id is None:
        return None
    attendees: tuple[UUID, ...] = ()
    if row.meeting_id is not None:
        attendees = tuple(
            p.person_id
            for p in await uow.session.execute(
                select(mp.c.person_id)
                .where(mp.c.user_id == uow.user_id, mp.c.meeting_id == row.meeting_id)
                .order_by(mp.c.person_id)
            )
        )
    speaker_people: dict[str, UUID] = {}
    if row.meeting_id is not None:
        for p in await uow.session.execute(
            select(mp.c.person_id, mp.c.speaker_labels).where(
                mp.c.user_id == uow.user_id,
                mp.c.meeting_id == row.meeting_id,
                mp.c.mapping_status == "applied",
            )
        ):
            for label in p.speaker_labels or []:
                speaker_people[label] = p.person_id
    segments = await load_segments(uow, recording_id, row.transcription_model)
    return TranscriptView(
        recording_id=row.id,
        source_item_id=row.source_item_id,
        meeting_id=row.meeting_id,
        title=row.meeting_title or row.title,
        starts_at=row.starts_at or row.occurred_at or row.created_at,
        transcription_model=row.transcription_model,
        transcript_hash=bytes(row.transcript_hash or b""),
        duration_s=float(row.duration_s) if row.duration_s is not None else None,
        segments=tuple(segments),
        attendee_ids=attendees,
        speaker_people=speaker_people,
    )
