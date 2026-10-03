"""Provider-neutral request, response and error types (BACKEND_DESIGN.md §5.4-§5.5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

Thinking = Literal["minimal", "low", "medium", "high"]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    audio_input_tokens: int = 0
    audio_seconds: float = 0.0


@dataclass(frozen=True)
class FileRef:
    """A file uploaded through the provider's Files API (audio for AI-09). ``sha256`` (hex) is the
    file's content digest: the cassette key uses it instead of the per-upload URI."""

    name: str
    uri: str
    mime_type: str
    sha256: str | None = None


@dataclass(frozen=True)
class GenerateRequest:
    model: str
    contents: tuple[str | FileRef, ...]
    system_instruction: str | None = None
    response_json_schema: dict[str, Any] | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    thinking: Thinking | None = None


@dataclass(frozen=True)
class GenerateResponse:
    text: str
    usage: Usage
    model_version: str | None = None
    finish_reason: str | None = None


@dataclass(frozen=True)
class EmbedRequest:
    model: str
    texts: tuple[str, ...]
    output_dimensionality: int | None = None
    task_type: str | None = None


@dataclass(frozen=True)
class EmbedResponse:
    vectors: tuple[tuple[float, ...], ...]
    usage: Usage = field(default_factory=Usage)


class CallStatus(StrEnum):
    """``ai_calls.status`` values (content-free outcome of one provider call)."""

    OK = "ok"
    SCHEMA_INVALID = "schema_invalid"
    SAFETY_BLOCK = "safety_block"
    EMPTY_RESPONSE = "empty_response"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    PROVIDER_ERROR = "provider_error"


class AIError(Exception):
    """Base class of AI-layer errors. Messages never contain prompt or output text."""


class UnknownRole(AIError):
    pass


class RoleDisabled(AIError):
    pass


class NoFallback(AIError):
    pass


class ConfigError(AIError):
    """Invalid ``config/models.yaml`` or ``config/pricing.yaml``: a startup error."""


class PriceNotFound(ConfigError):
    pass


class ProviderError(AIError):
    """A provider call failed. ``status`` is what the meter records."""

    status: CallStatus = CallStatus.PROVIDER_ERROR

    def __init__(
        self, message: str = "", *, code: int | None = None, retry_after_s: float | None = None
    ) -> None:
        super().__init__(message or self.status.value)
        self.code = code
        self.retry_after_s = retry_after_s


class ProviderTimeout(ProviderError):
    status = CallStatus.TIMEOUT


class ProviderRateLimited(ProviderError):
    status = CallStatus.RATE_LIMITED


class ProviderUnavailable(ProviderError):
    """5xx or transport failure."""

    status = CallStatus.PROVIDER_ERROR


class ProviderRejected(ProviderError):
    """4xx other than 429 (bad request, unknown model, permission)."""

    status = CallStatus.PROVIDER_ERROR


class SafetyBlocked(ProviderError):
    status = CallStatus.SAFETY_BLOCK


class EmptyResponse(ProviderError):
    status = CallStatus.EMPTY_RESPONSE


class SchemaInvalid(AIError):
    """The response is not valid JSON for the output schema (repairable once, AI_PIPELINE.md §7)."""

    status = CallStatus.SCHEMA_INVALID

    def __init__(self, summary: str, *, finish_reason: str | None = None) -> None:
        super().__init__(summary)
        self.summary = summary  # field paths and error types only, never output text
        self.finish_reason = finish_reason  # MAX_TOKENS: the output limit cut the JSON (AI-09 windows)


class CassetteMiss(AIError):
    """Replay mode found no recorded response for the request; the network is never used."""
