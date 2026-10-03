"""AI-09 ``transcribe`` (AI_PIPELINE.md §5.10): one speech-model call over a Files API audio file.

``intelligence`` never reads SOURCE tables (BACKEND_DESIGN.md §6.3): the caller (``meetings``)
prepares the audio, uploads it through :meth:`AIClient.upload_file` and passes the reference.
This module makes the call and reports what happened; the caller validates the segments,
decides between the single call and the 60-minute window fallback, counts attempts (media
class) and stores the transcript. No database access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from eca.intelligence.extraction import prompt_text
from eca.intelligence.output_schemas.transcribe import SCHEMA_VERSION as TRANSCRIBE_SCHEMA
from eca.intelligence.output_schemas.transcribe import SegmentOut, Transcription
from eca.intelligence.provider.client import AIClient
from eca.intelligence.provider.types import AIError, FileRef, ProviderError, SchemaInvalid
from eca.platform.errors import BudgetExceeded

TRANSCRIBE_PROMPT_VERSION = "transcribe/v1"
OUTPUT_LIMIT_REASONS = frozenset({"MAX_TOKENS", "FinishReason.MAX_TOKENS"})
_RETRYABLE = frozenset({"timeout", "rate_limited", "provider_error"})


@dataclass
class TranscribeOutcome:
    ok: bool
    segments: list[SegmentOut] = field(default_factory=list)
    truncated: bool = False  # the output limit was reached: use 60-minute windows
    model: str | None = None
    error_code: str | None = None
    retryable: bool = False
    deferred_for_s: int | None = None  # budget refusal: not an attempt (AI_PIPELINE.md §7)
    call_ids: list[UUID] = field(default_factory=list)


async def run_transcribe(
    client: AIClient,
    audio: FileRef,
    *,
    duration_s: float,
    user_id: UUID | None,
    attempt: int,
    use_fallback: bool,
) -> TranscribeOutcome:
    """One AI-09 call. ``duration_s`` is metered as ``audio_seconds`` (per-minute prices)."""
    system = prompt_text(TRANSCRIBE_PROMPT_VERSION)
    try:
        result = await client.generate(
            "transcribe",
            prompt_version=TRANSCRIBE_PROMPT_VERSION,
            schema_version=TRANSCRIBE_SCHEMA,
            output_model=Transcription,
            contents=[audio, "Transcribe this audio."],
            system_instruction=system,
            user_id=user_id,
            attempt=attempt,
            use_fallback=use_fallback,
            audio_seconds=duration_s,
        )
    except BudgetExceeded as exc:
        return TranscribeOutcome(
            ok=False, error_code="budget_exceeded", deferred_for_s=exc.retry_after_s or 3600
        )
    except SchemaInvalid as exc:
        if exc.finish_reason in OUTPUT_LIMIT_REASONS:
            return TranscribeOutcome(ok=False, truncated=True, error_code="output_limit")
        return TranscribeOutcome(ok=False, error_code="schema_invalid", retryable=True)
    except ProviderError as exc:
        return TranscribeOutcome(
            ok=False, error_code=type(exc).__name__, retryable=exc.status.value in _RETRYABLE
        )
    except AIError as exc:  # role disabled, unknown role, cassette miss
        return TranscribeOutcome(ok=False, error_code=type(exc).__name__)
    out = TranscribeOutcome(ok=True, segments=list(result.output.segments), model=result.model)
    if result.call_id is not None:
        out.call_ids.append(result.call_id)
    if not result.output.complete or (result.finish_reason or "") in OUTPUT_LIMIT_REASONS:
        out.ok, out.truncated, out.error_code = False, True, "output_limit"
    return out
