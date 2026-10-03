"""Phase 4 pure functions (IMPLEMENTATION_PLAN.md §0.8 deferred unit tests).

Transcript parsers, AI-09 segment validation, probe parsing and window planning, media limits,
speaker matching, upload token signing and the local storage adapter, transcript chunk windows,
transcript grounding, the prep cache key, prep ask citations and the net-change diff. All inputs
are synthetic.
"""

from __future__ import annotations

import datetime
import io
import itertools
import json
import uuid
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from eca.attention.prep import cache_key
from eca.meetings.media import MediaError, MediaTools, extract_args, parse_probe, window_starts
from eca.meetings.recordings import (
    MAX_MEDIA_BYTES,
    MAX_TRANSCRIPT_BYTES,
    classify_upload,
    parse_sha256,
    storage_key,
)
from eca.meetings.speakers import (
    Attendee,
    elimination,
    introduced_names,
    introduction_proposals,
    match_attendees,
    unmatched_introductions,
)
from eca.meetings.transcripts import (
    DOCX_MAX_RATIO,
    Segment,
    TranscriptParseError,
    duration_of,
    parse_docx,
    parse_srt,
    parse_transcript_file,
    parse_txt,
    parse_vtt,
    transcript_hash,
    validate_model_segments,
)
from eca.platform.errors import PermissionDenied, ValidationFailed
from eca.platform.storage import (
    LocalObjectStorage,
    UploadGrant,
    UploadSigner,
    build_storage,
    check_key,
    sha256_of,
)
from eca.retrieval.asks import keep_cited, section_lines
from eca.retrieval.chunking import TranscriptLine, estimate_tokens, transcript_chunks
from eca.work.meeting_apply import ground_segment
from eca.work.meeting_read import item_change
from eca.work.read import TimelineEvent

T0 = datetime.datetime(2026, 9, 21, 9, 0, tzinfo=datetime.UTC)
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _rows(segments: list[Segment]) -> list[tuple[int | None, int | None, str | None, str]]:
    return [(s.start_ms, s.end_ms, s.speaker_label, s.text) for s in segments]


# ---------------------------------------------------------------- transcript parsers


def test_vtt_voice_tags_name_prefixes_and_timings() -> None:
    data = (
        b"WEBVTT\n\n"
        b"1\n00:00:01.000 --> 00:00:04.500\n<v Priya Raman>Let's start with the <b>budget</b>.</v>\n\n"
        b"00:00:05.000 --> 00:00:07.250\nSam: I'll send the draft\nby Friday.\n\n"
        b"01:02:03.004 --> 01:02:05.000\nno speaker here\n"
    )
    segments = parse_vtt(data)
    assert [s.seq for s in segments] == [0, 1, 2]
    assert _rows(segments) == [
        (1000, 4500, "Priya Raman", "Let's start with the budget."),
        (5000, 7250, "Sam", "I'll send the draft by Friday."),
        (3723004, 3725000, None, "no speaker here"),
    ]


def test_vtt_needs_its_header() -> None:
    with pytest.raises(TranscriptParseError, match="WEBVTT"):
        parse_vtt(b"00:00:01.000 --> 00:00:02.000\nHello\n")


def test_srt_comma_milliseconds_and_name_prefix() -> None:
    data = (
        b"1\r\n00:00:01,500 --> 00:00:03,000\r\nAlex Chen: Can we move the review?\r\n\r\n"
        b"2\r\n00:00:04,000 --> 00:00:02,000\r\nSure.\r\n"
    )
    assert _rows(parse_srt(data)) == [
        (1500, 3000, "Alex Chen", "Can we move the review?"),
        (4000, 4000, None, "Sure."),  # end before start is clamped to start
    ]


def test_txt_stamps_names_and_paragraph_ends() -> None:
    data = (
        b"[00:00:10] Priya: Welcome everyone.\n\n[00:01:00] Sam: Thanks.\n\n"
        b"Unstamped note.\n\n[00:02:30] Done."
    )
    assert _rows(parse_txt(data)) == [
        (10_000, 60_000, "Priya", "Welcome everyone."),
        (60_000, 150_000, "Sam", "Thanks."),
        (None, None, None, "Unstamped note."),
        (150_000, 150_000, None, "Done."),  # the last stamped paragraph ends where it starts
    ]


def test_txt_one_line_per_utterance_without_timing() -> None:
    segments = parse_txt(b"Priya: first point\nSam: second point\n")
    assert _rows(segments) == [(None, None, "Priya", "first point"), (None, None, "Sam", "second point")]
    assert duration_of(segments) is None


def test_txt_decodes_utf16_and_rejects_binary_and_empty() -> None:
    assert parse_txt("Zoë: café".encode("utf-16"))[0].text == "café"
    with pytest.raises(TranscriptParseError):
        parse_txt(b"\x00\x00\x01\x02\xff\xfe")  # a NUL character in both decodings
    with pytest.raises(TranscriptParseError, match="no transcript text"):
        parse_txt(b"\n\n   \n")


def _docx(paragraphs: list[str], *, extra: bytes = b"") -> bytes:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = extra + f'<w:document xmlns:w="{w}"><w:body>{body}</w:body></w:document>'.encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def test_docx_paragraphs_follow_the_txt_rules() -> None:
    data = _docx(["[00:00:05] Priya: Kickoff.", "", "Sam: Agreed."])
    assert parse_transcript_file(DOCX_MIME, data) == (
        "file:docx",
        [Segment(0, 5000, 5000, "Priya", "Kickoff."), Segment(1, None, None, "Sam", "Agreed.")],
    )


def test_docx_limits_refuse_zip_bombs_dtds_and_non_docx() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", b"<a>" + b" " * (DOCX_MAX_RATIO * 2000) + b"</a>")
    with pytest.raises(TranscriptParseError, match="ratio"):
        parse_docx(buf.getvalue())
    with pytest.raises(TranscriptParseError, match="document type"):
        parse_docx(_docx(["x"], extra=b'<!DOCTYPE d [<!ENTITY e "boom">]>'))
    with pytest.raises(TranscriptParseError, match="not a DOCX"):
        parse_docx(b"plain text")
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("other.xml", b"<a/>")
    with pytest.raises(TranscriptParseError, match=r"word/document\.xml"):
        parse_docx(empty.getvalue())


def test_parse_transcript_file_by_type() -> None:
    assert parse_transcript_file("text/vtt", b"WEBVTT\n\n00:01.000 --> 00:02.000\nHi")[0] == "file:vtt"
    assert (
        parse_transcript_file("application/x-subrip", b"1\n00:00:01,000 --> 00:00:02,000\nHi")[0]
        == "file:srt"
    )
    with pytest.raises(TranscriptParseError, match="unsupported"):
        parse_transcript_file("application/pdf", b"%PDF")


def test_transcript_hash_and_duration() -> None:
    a = [Segment(0, 0, 1000, "A", "one"), Segment(1, 1000, 2500, "B", "two")]
    b = [Segment(0, 0, 1000, "A", "one"), Segment(1, 1000, 2500, "B", "two!")]
    assert transcript_hash(a) == transcript_hash(list(a))
    assert transcript_hash(a) != transcript_hash(b)
    assert len(transcript_hash(a)) == 32
    assert duration_of(a) == 2.5


def test_ai09_segment_validation_places_windows_and_drops_bad_rows() -> None:
    raw = [
        (5000, 4000, "Speaker 1", "end before start"),
        (1000, 2000, "Speaker 2", "  "),
        (-500, 100, "Speaker 1", "negative start"),
        (0, 999_999, "Speaker 1", "runs past the end"),
        (50_000, 51_000, "Speaker 2", "beyond duration + slack"),
    ]
    out = validate_model_segments(raw, duration_ms=10_000)
    assert out == [
        (0, 12_000, "Speaker 1", "runs past the end"),
        (5000, 5000, "Speaker 1", "end before start"),
    ]
    # A later 60-minute window: checks use window-relative times, then the offset places the rows.
    windowed = validate_model_segments(raw, duration_ms=10_000, offset_ms=3_600_000, label_prefix="W2 ")
    assert windowed == [
        (3_600_000, 3_612_000, "W2 Speaker 1", "runs past the end"),
        (3_605_000, 3_605_000, "W2 Speaker 1", "end before start"),
    ]
    late = validate_model_segments(
        [(1_800_000, 1_805_000, "Speaker 1", "half an hour into window 2")],
        duration_ms=3_600_000,
        offset_ms=3_600_000,
        label_prefix="W2 ",
    )
    assert late == [(5_400_000, 5_405_000, "W2 Speaker 1", "half an hour into window 2")]


# ---------------------------------------------------------------- media


def test_parse_probe_duration_audio_and_errors() -> None:
    probe = {"format": {"duration": "125.4"}, "streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}
    assert parse_probe(json.dumps(probe).encode()).duration_s == pytest.approx(125.4)
    from_streams = {
        "format": {},
        "streams": [{"codec_type": "audio", "duration": "N/A"}, {"codec_type": "audio", "duration": "30.5"}],
    }
    assert parse_probe(json.dumps(from_streams).encode()).duration_s == pytest.approx(30.5)
    silent = parse_probe(
        json.dumps({"format": {"duration": "10"}, "streams": [{"codec_type": "video"}]}).encode()
    )
    assert silent.has_audio is False
    for bad in (b"not json", json.dumps({"streams": [{"codec_type": "audio"}]}).encode()):
        with pytest.raises(MediaError) as err:
            parse_probe(bad)
        assert (err.value.code, err.value.retryable) == ("unreadable", False)


def test_window_starts_cover_the_duration_in_60_minute_windows() -> None:
    assert window_starts(0) == [0]
    assert window_starts(3600) == [0]
    assert window_starts(3600.5) == [0, 3600]
    assert window_starts(10_800) == [0, 3600, 7200]


def test_extract_args_keep_audio_only_and_never_use_a_shell() -> None:
    args = extract_args(MediaTools(ffmpeg="ffmpeg"), Path("in.mp4"), Path("out.ogg"))
    assert args[0] == "ffmpeg" and args[-1] == "out.ogg"
    joined = " ".join(args)
    for flag in ("-map 0:a:0", "-vn", "-sn", "-dn", "-ac 1", "-ar 16000", "-c:a libopus", "-nostdin"):
        assert flag in joined


def test_upload_limits_types_and_keys() -> None:
    assert classify_upload("audio/mpeg", 1) == "media"
    assert classify_upload("Video/MP4; codecs=avc1", MAX_MEDIA_BYTES) == "media"
    assert classify_upload("text/vtt", MAX_TRANSCRIPT_BYTES) == "transcript_file"
    for mime, size, reason in (
        ("audio/mpeg", 0, "size"),
        ("audio/mpeg", MAX_MEDIA_BYTES + 1, "size"),
        ("text/plain", MAX_TRANSCRIPT_BYTES + 1, "size"),
        ("application/pdf", 10, "type"),
    ):
        with pytest.raises(ValidationFailed) as err:
            classify_upload(mime, size)
        assert err.value.details == {"reason": reason}
    assert parse_sha256(" " + "AB" * 32 + " ") == bytes.fromhex("ab" * 32)
    with pytest.raises(ValidationFailed):
        parse_sha256("xyz")
    user, rec = uuid.uuid4(), uuid.uuid4()
    assert check_key(storage_key(user, rec, "audio.ogg")) == f"recordings/{user}/{rec}/audio.ogg"


# ---------------------------------------------------------------- speaker matching

PRIYA, SAM, ALEX, ME = (uuid.UUID(int=i) for i in (1, 2, 3, 4))
ATTENDEES = [
    Attendee(PRIYA, ("priya raman", "priya"), False),
    Attendee(SAM, ("sam okafor",), False),
    Attendee(ALEX, ("alex chen",), False),
    Attendee(ME, ("avery lindqvist",), True),
]


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("Hi all, I'm Priya and I run finance.", ["Priya"]),
        ("this is Priya Raman from finance", ["Priya Raman"]),
        ("This is Sam.", ["Sam"]),
        ("My name is Alex Chen.", ["Alex Chen"]),
        ("Priya here, can you hear me?", ["Priya"]),
        ("Hello, Sam here.", ["Sam"]),
        ("I'm going to share my screen.", []),
        ("I'm sorry, I'm not sure.", []),
        ("everyone here? let's start", []),
    ],
)
def test_introduced_names(text: str, names: list[str]) -> None:
    assert introduced_names([text]) == names


def test_match_attendees_full_name_then_unique_first_name() -> None:
    assert match_attendees("Priya Raman", ATTENDEES) == [PRIYA]
    assert match_attendees("sam", ATTENDEES) == [SAM]
    assert match_attendees("Jordan", ATTENDEES) == []
    twins = [*ATTENDEES, Attendee(uuid.UUID(int=9), ("sam lee",), False)]
    assert match_attendees("Sam", twins) == sorted([SAM, uuid.UUID(int=9)])


def test_introductions_only_count_in_a_labels_first_three_segments() -> None:
    segments = [
        Segment(0, 0, 1, "S1", "Good morning."),
        Segment(1, 1, 2, "S2", "Hi, I'm Sam."),
        Segment(2, 2, 3, "S1", "Two."),
        Segment(3, 3, 4, "S1", "Three."),
        Segment(4, 4, 5, "S1", "By the way, I'm Priya."),  # fourth segment of S1: ignored
        Segment(5, 5, 6, "S3", "This is Jordan Blake."),
    ]
    proposals = introduction_proposals(segments, ATTENDEES)
    assert [(p.label, p.person_id, p.confidence, p.method) for p in proposals] == [
        ("S2", SAM, 0.95, "self_introduction")
    ]
    assert unmatched_introductions(segments, ATTENDEES) == {"S3": "Jordan Blake"}


def test_elimination_rule() -> None:
    labels = ["A", "B", "C"]
    two = [Attendee(PRIYA, ("priya",), False), Attendee(SAM, ("sam",), False), Attendee(ME, ("me",), True)]
    p = elimination(labels, {"A": PRIYA, "B": ME}, two, ME)
    assert p is not None and (p.label, p.person_id, p.confidence) == ("C", SAM, 0.9)
    # the user attends but is not mapped yet: no elimination
    assert elimination(labels, {"A": PRIYA, "B": SAM}, two, ME) is None
    # two labels unmapped
    assert elimination(labels, {"A": PRIYA}, two, ME) is None
    # two attendees other than the user unmapped
    assert elimination(labels, {"A": ME, "B": ALEX}, two, ME) is None
    # the user is absent from the attendees: allowed
    others = [a for a in two if not a.is_self]
    q = elimination(["A", "B"], {"A": PRIYA}, others, ME)
    assert q is not None and q.person_id == SAM


# ---------------------------------------------------------------- upload tokens and local storage

KEY = "recordings/" + str(uuid.UUID(int=1)) + "/" + str(uuid.UUID(int=2))


def test_upload_token_round_trip_and_rejections() -> None:
    signer = UploadSigner(b"k" * 32)
    grant = UploadGrant(KEY, "audio/mpeg", 1000, T0 + datetime.timedelta(hours=1))
    token = signer.sign(grant)
    assert signer.verify(token, now=T0) == grant
    with pytest.raises(PermissionDenied, match="expired"):
        signer.verify(token, now=T0 + datetime.timedelta(hours=1))
    with pytest.raises(PermissionDenied):
        UploadSigner(b"x" * 32).verify(token, now=T0)
    body, mac = token.split(".")
    forged = UploadSigner(b"x" * 32).sign(UploadGrant(KEY, "audio/mpeg", 10**12, grant.expires_at))
    with pytest.raises(PermissionDenied):
        signer.verify(f"{forged.split('.')[0]}.{mac}", now=T0)  # payload swapped, old signature
    for bad in ("", "abc", "a.b.c", body):
        with pytest.raises(PermissionDenied):
            signer.verify(bad, now=T0)
    with pytest.raises(ValueError):
        UploadSigner(b"short")


@pytest.mark.parametrize(
    "key", ["../etc/passwd", "recordings/../x", "recordings/abc", "Recordings/" + str(uuid.UUID(int=1))]
)
def test_object_keys_hold_ids_only(key: str) -> None:
    with pytest.raises(ValueError):
        check_key(key)


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for p in parts:
        yield p


async def test_local_storage_receive_enforces_type_and_size(tmp_path: Path) -> None:
    store = LocalObjectStorage(tmp_path, UploadSigner(b"k" * 32))
    upload = store.presign_put(KEY, content_type="audio/mpeg", max_bytes=8, now=T0)
    token = upload.url.rsplit("/", 1)[1]
    assert upload.method == "PUT" and upload.headers == {"Content-Type": "audio/mpeg"}
    with pytest.raises(ValidationFailed, match="Content-Type"):
        await store.receive(token, content_type="video/mp4", chunks=_chunks(b"x"), now=T0)
    with pytest.raises(ValidationFailed, match="larger"):
        await store.receive(token, content_type="audio/mpeg", chunks=_chunks(b"12345", b"6789"), now=T0)
    assert await store.head(KEY) is None  # nothing stored, no partial file left
    assert not store.path(KEY).with_name(store.path(KEY).name + ".part").exists()
    info = await store.receive(
        token, content_type="audio/mpeg; x=1", chunks=_chunks(b"1234", b"5678"), now=T0
    )
    assert info.size == 8 and (await store.head(KEY)) is not None
    assert (
        sha256_of(store.path(KEY)).hex() == "ef797c8118f02dfb649607dd5d3f8c7623048c9c063d532cc95c5ed7a898a64f"
    )
    await store.delete(KEY)
    await store.delete(KEY)  # idempotent
    assert await store.head(KEY) is None


def test_local_storage_is_refused_in_production() -> None:
    settings = SimpleNamespace(
        api_storage_backend="local", is_production=True, api_storage_local_dir=None, storage_signing_key=None
    )
    with pytest.raises(ValueError, match="development only"):
        build_storage(settings)  # type: ignore[arg-type]
    settings.is_production = False
    settings.storage_signing_key = SecretStr("s" * 40)
    assert isinstance(build_storage(settings), LocalObjectStorage)  # type: ignore[arg-type]


# ---------------------------------------------------------------- transcript chunk windows


def test_transcript_windows_group_turns_and_overlap_one_turn() -> None:
    lines = []
    for i in range(40):
        speaker = "Priya" if (i // 2) % 2 == 0 else "Sam"
        lines.append(TranscriptLine(i * 10_000, i * 10_000 + 9_000, speaker, f"Sentence {i} " + "word " * 60))
    chunks = transcript_chunks("Weekly sync", lines)
    assert len(chunks) > 1
    assert all(c.kind == "transcript" and c.title == "Weekly sync" for c in chunks)
    assert all(c.token_count <= 800 for c in chunks)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    # consecutive lines of one speaker form one turn: "Priya" turns hold two lines each
    assert chunks[0].text.startswith("[00:00] Priya: Sentence 0 ")
    assert "Sentence 1 " in chunks[0].text.split("\n")[0]
    # one-turn overlap: each window starts with the previous window's last turn
    for prev, nxt in itertools.pairwise(chunks):
        assert nxt.text.split("\n")[0] == prev.text.split("\n")[-1]
    assert chunks[0].start_ms == 0
    assert chunks[-1].end_ms == 39 * 10_000 + 9_000


def test_transcript_windows_small_inputs_and_missing_timing() -> None:
    lines = [TranscriptLine(None, None, "S1", "Short."), TranscriptLine(None, None, "S2", "Also short.")]
    chunks = transcript_chunks(None, lines)
    assert len(chunks) == 1 and chunks[0].text == "S1: Short.\nS2: Also short."
    assert (chunks[0].start_ms, chunks[0].end_ms, chunks[0].title) == (None, None, None)
    assert chunks[0].token_count == estimate_tokens(chunks[0].text)
    assert transcript_chunks("t", []) == []


def test_a_long_single_turn_is_split_at_sentence_ends() -> None:
    body = " ".join(f"This is sentence number {i} of a very long monologue." for i in range(400))
    chunks = transcript_chunks("t", [TranscriptLine(0, 1000, "S1", body)])
    assert len(chunks) > 1 and all(c.token_count <= 800 for c in chunks)


# ---------------------------------------------------------------- grounding


def _evidence(seq: int, quote: str, start_ms: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(segment_seq=seq, quote=quote, start_ms=start_ms)


SEGMENTS = {
    0: Segment(0, 0, 4000, "S1", "We agreed to ship the beta on Friday."),
    1: Segment(1, 4000, 9000, "S2", "Then I will update the pricing page."),
}


def test_grounding_verbatim_fuzzy_joined_and_missing() -> None:
    g = ground_segment(_evidence(0, "ship the beta on Friday", 0), SEGMENTS, duration_ms=9000)  # type: ignore[arg-type]
    assert g is not None and (g.seq, g.start_ms, g.end_ms, g.fuzzy) == (0, 0, 4000, False)
    g = ground_segment(_evidence(0, "SHIP  the beta"), SEGMENTS, duration_ms=9000)  # type: ignore[arg-type]
    assert g is not None and g.fuzzy is True
    g = ground_segment(_evidence(0, "on Friday. Then I will update"), SEGMENTS, duration_ms=9000)  # type: ignore[arg-type]
    assert g is not None and (g.seq, g.end_ms) == (0, 9000)
    g = ground_segment(_evidence(0, "ship the beta", 60_000), SEGMENTS, duration_ms=9000)  # type: ignore[arg-type]
    assert g is not None and g.fuzzy is True  # timestamp far from the segment
    assert ground_segment(_evidence(0, "cancel the beta"), SEGMENTS, duration_ms=9000) is None  # type: ignore[arg-type]
    assert ground_segment(_evidence(7, "ship"), SEGMENTS, duration_ms=9000) is None  # type: ignore[arg-type]
    assert ground_segment(_evidence(1, "update the pricing page"), SEGMENTS, duration_ms=None) is not None  # type: ignore[arg-type]


# ---------------------------------------------------------------- prep cache key and asks


def test_prep_cache_key_is_canonical_and_change_sensitive() -> None:
    a = {"meeting": "m1", "version": 3, "entities": [["work_item", "i1", 2]], "prior": ["p1"]}
    b = {"prior": ["p1"], "entities": [["work_item", "i1", 2]], "version": 3, "meeting": "m1"}
    assert cache_key(a) == cache_key(b)
    assert len(cache_key(a)) == 64
    assert cache_key(a) != cache_key({**a, "entities": [["work_item", "i1", 3]]})
    assert cache_key(a) != cache_key({**a, "version": 4})


def test_prep_section_lines_and_cited_asks() -> None:
    sections = {
        "purpose": {"title": "Pricing review", "description": "Q4 tiers", "attendees": ["Priya", "Sam"]},
        "open_items_mine": [{"id": "i1", "title": "Send the deck", "owner": "you", "due_text": "Friday"}],
        "open_items_theirs": [
            {"id": "i2", "title": "Legal sign-off", "owner": "Sam", "verification_status": "suggested"}
        ],
        "unresolved_questions": [{"id": "d1", "statement": "Annual discount?"}],
    }
    lines = section_lines(sections)
    assert lines[0].cid == "S1" and lines[0].section == "purpose"
    assert [line.cid for line in lines] == [f"S{i}" for i in range(1, len(lines) + 1)]
    assert {line.entity_id for line in lines[1:]} <= {"i1", "i2", "d1"}
    asks = [
        SimpleNamespace(text="Confirm the deck date", citations=[lines[-1].cid, "S99"]),
        SimpleNamespace(text="Uncited ask", citations=["S42"]),
    ]
    kept = keep_cited(asks, lines)
    assert [a["text"] for a in kept] == ["Confirm the deck date"]
    assert kept[0]["citations"] == [lines[-1].cid] and kept[0]["kind"] == "recommendation"


# ---------------------------------------------------------------- net-change diff


ITEM = uuid.UUID(int=77)


def _event(
    minutes: int, event_type: str, sets: dict[str, object], *, actor: str = "user", materiality: int = 3
) -> TimelineEvent:
    at = T0 + datetime.timedelta(minutes=minutes)
    return TimelineEvent(
        id=uuid.UUID(int=1000 + minutes),
        entity_type="work_item",
        entity_id=ITEM,
        event_type=event_type,
        actor=actor,
        authority=5 if actor == "user" else 2,
        materiality=materiality,
        occurred_at=at,
        recorded_at=at,
        payload={"set": sets},
        evidence_id=None,
    )


def test_item_change_new_deadline_status_and_immaterial() -> None:
    created = _event(
        0, "created", {"title": "Ship beta", "lifecycle_status": "open", "direction": "my_commitment"}
    )
    moved = _event(90, "deadline_changed", {"due_at": "2026-09-30T00:00:00+00:00"})
    done = _event(120, "completed", {"lifecycle_status": "done"})
    noise = _event(100, "noted", {"notes": "x"}, materiality=1)
    since, until = T0 + datetime.timedelta(minutes=60), T0 + datetime.timedelta(minutes=110)

    new = item_change([created], since=T0 - datetime.timedelta(minutes=1), until=until)
    assert new is not None and new.kind == "new" and new.before == {}

    deadline = item_change([created, moved, noise], since=since, until=until)
    assert deadline is not None and deadline.kind == "deadline"
    assert deadline.before == {"due_at": None} and deadline.after == {"due_at": "2026-09-30T00:00:00+00:00"}
    assert deadline.events == 2

    status = item_change([created, done], since=since, until=T0 + datetime.timedelta(minutes=130))
    assert status is not None and status.kind == "status"
    assert status.after == {"lifecycle_status": "done"}

    assert item_change([created, noise], since=since, until=until) is None  # nothing material
    assert item_change([created, moved], since=until, until=until + datetime.timedelta(minutes=5)) is None
