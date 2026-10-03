"""ffprobe and ffmpeg for uploaded media (slice 4.2, TECHNICAL_DESIGN.md §16.1).

Runs only in the worker (queue ``media``), never inside a database transaction and never in the
API process. Arguments are fixed lists (no shell); file names are generated here, never taken
from the user. Every process has a timeout and is killed when it expires. Only the first audio
stream is extracted (``-vn``): video frames never leave this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

PROBE_TIMEOUT_S = 60
MIN_EXTRACT_TIMEOUT_S = 600
WINDOW_S = 3600  # the 60-minute window fallback of AI-09
AUDIO_MIME = "audio/ogg"


class MediaError(Exception):
    """A media step failed. ``code`` is a stable error code; ``retryable`` decides between a
    stage retry (tool missing, timeout) and rejection (unreadable input)."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class Probe:
    duration_s: float
    has_audio: bool


@dataclass(frozen=True)
class MediaTools:
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"


@contextlib.contextmanager
def job_dir(base: str | None) -> Iterator[Path]:
    """One temporary directory per job, removed on success, failure and timeout."""
    path = Path(tempfile.mkdtemp(prefix="eca-media-", dir=base))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


async def _run(args: list[str], *, timeout_s: float) -> bytes:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise MediaError("media_tool_missing", retryable=True) from exc
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise MediaError("media_timeout", retryable=True) from exc
    if proc.returncode != 0:
        raise MediaError("unreadable", retryable=False)
    return out


def _nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def parse_probe(raw: bytes) -> Probe:
    """Pure: ffprobe JSON → duration and whether an audio stream exists."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MediaError("unreadable", retryable=False) from exc
    streams = data.get("streams") or []
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    duration = (data.get("format") or {}).get("duration")
    if duration is None:
        durations = [float(s["duration"]) for s in streams if s.get("duration") not in (None, "N/A")]
        duration = max(durations) if durations else None
    try:
        seconds = float(duration) if duration is not None else 0.0
    except (TypeError, ValueError):
        seconds = 0.0
    if has_audio and seconds <= 0:
        raise MediaError("unreadable", retryable=False)
    return Probe(duration_s=seconds, has_audio=has_audio)


async def probe(tools: MediaTools, path: Path) -> Probe:
    raw = await _run(
        [tools.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        timeout_s=PROBE_TIMEOUT_S,
    )
    return parse_probe(raw)


def extract_args(tools: MediaTools, source: Path, target: Path) -> list[str]:
    """Mono 16 kHz Opus, first audio stream only, no video, subtitles or data streams."""
    return [
        tools.ffmpeg, "-nostdin", "-y", "-i", str(source),
        "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k", "-application", "voip",
        str(target),
    ]  # fmt: skip


async def extract_audio(tools: MediaTools, source: Path, target: Path, *, duration_s: float) -> None:
    await _run(extract_args(tools, source, target), timeout_s=max(MIN_EXTRACT_TIMEOUT_S, duration_s))
    if not _nonempty(target):
        raise MediaError("no_audio", retryable=False)


def window_starts(duration_s: float, window_s: int = WINDOW_S) -> list[int]:
    """Start offsets (seconds) of the 60-minute windows covering the duration."""
    count = max(1, int(-(-duration_s // window_s)))
    return [i * window_s for i in range(count)]


async def cut_window(tools: MediaTools, source: Path, target: Path, *, start_s: int, length_s: int) -> None:
    """One window of the prepared audio, by stream copy (no re-encoding)."""
    await _run(
        [
            tools.ffmpeg,
            "-nostdin",
            "-y",
            "-ss",
            str(start_s),
            "-t",
            str(length_s),
            "-i",
            str(source),
            "-c",
            "copy",
            str(target),
        ],
        timeout_s=MIN_EXTRACT_TIMEOUT_S,
    )
    if not _nonempty(target):
        raise MediaError("unreadable", retryable=False)
