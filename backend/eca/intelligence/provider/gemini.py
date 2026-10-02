"""Gemini wrapper over ``google-genai`` (TECHNICAL_DESIGN.md §10.5, BACKEND_DESIGN.md §5.4).

Stateless ``generateContent`` with structured output (JSON schema), embeddings, the Files API,
and ``models.get`` for the smoke test. This is the only module that imports ``google.genai``
(import-linter contract). Errors are mapped to provider-neutral types; messages never contain
prompt or output text, and the API key is never logged.

The Gemini API returns no token counts for embeddings; their input tokens are estimated as
``ceil(characters / 4)`` for metering.
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import Any

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import SecretStr

from eca.intelligence.provider.types import (
    EmbedRequest,
    EmbedResponse,
    EmptyResponse,
    FileRef,
    GenerateRequest,
    GenerateResponse,
    ProviderError,
    ProviderRateLimited,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
    SafetyBlocked,
    Usage,
)

_THINKING = {
    "minimal": genai_types.ThinkingLevel.MINIMAL,
    "low": genai_types.ThinkingLevel.LOW,
    "medium": genai_types.ThinkingLevel.MEDIUM,
    "high": genai_types.ThinkingLevel.HIGH,
}
_SAFETY_FINISH = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}


def _map_error(exc: BaseException) -> ProviderError:
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return ProviderTimeout("provider call timed out")
    if isinstance(exc, genai_errors.ClientError):
        if exc.code == 429:
            return ProviderRateLimited("rate limited", code=429)
        return ProviderRejected(f"provider rejected the request (HTTP {exc.code})", code=exc.code)
    if isinstance(exc, genai_errors.ServerError):
        return ProviderUnavailable(f"provider error (HTTP {exc.code})", code=exc.code)
    if isinstance(exc, genai_errors.APIError):
        return ProviderUnavailable(f"provider API error (HTTP {exc.code})", code=exc.code)
    if isinstance(exc, httpx.TransportError):
        return ProviderUnavailable(f"transport error ({type(exc).__name__})")
    return ProviderUnavailable(f"unexpected provider failure ({type(exc).__name__})")


def _usage(metadata: Any) -> Usage:
    if metadata is None:
        return Usage()
    audio = 0
    for detail in getattr(metadata, "prompt_tokens_details", None) or []:
        if getattr(detail, "modality", None) == genai_types.MediaModality.AUDIO:
            audio += int(detail.token_count or 0)
    return Usage(
        input_tokens=int(metadata.prompt_token_count or 0),
        cached_input_tokens=int(metadata.cached_content_token_count or 0),
        output_tokens=int(metadata.candidates_token_count or 0),
        thinking_tokens=int(metadata.thoughts_token_count or 0),
        audio_input_tokens=audio,
    )


def _part(content: str | FileRef) -> genai_types.Part:
    if isinstance(content, FileRef):
        return genai_types.Part.from_uri(file_uri=content.uri, mime_type=content.mime_type)
    return genai_types.Part.from_text(text=content)


class GeminiProvider:
    """Async Gemini client. ``client`` may be injected (tests use a fake with the same shape)."""

    def __init__(self, api_key: SecretStr | None, *, timeout_s: float, client: Any | None = None) -> None:
        if client is None:
            if api_key is None or not api_key.get_secret_value():
                raise ProviderRejected("GEMINI_API_KEY is not configured")
            client = genai.Client(
                api_key=api_key.get_secret_value(),
                http_options=genai_types.HttpOptions(timeout=int(timeout_s * 1000)),
            )
        self._client = client
        self._timeout_s = timeout_s

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        config = genai_types.GenerateContentConfig(
            system_instruction=request.system_instruction,
            temperature=request.temperature,
            max_output_tokens=request.max_output_tokens,
            response_mime_type="application/json" if request.response_json_schema is not None else None,
            response_json_schema=request.response_json_schema,
            thinking_config=(
                genai_types.ThinkingConfig(thinking_level=_THINKING[request.thinking])
                if request.thinking
                else None
            ),
        )
        contents = [genai_types.Content(role="user", parts=[_part(c) for c in request.contents])]
        try:
            async with asyncio.timeout(self._timeout_s):
                response = await self._client.aio.models.generate_content(
                    model=request.model, contents=contents, config=config
                )
        except Exception as exc:
            raise _map_error(exc) from exc

        feedback = getattr(response, "prompt_feedback", None)
        if feedback is not None and getattr(feedback, "block_reason", None):
            raise SafetyBlocked("prompt blocked by provider safety filters")
        candidates = getattr(response, "candidates", None) or []
        finish = getattr(candidates[0], "finish_reason", None) if candidates else None
        finish_name = getattr(finish, "name", None) or (str(finish) if finish else None)
        if finish_name in _SAFETY_FINISH:
            raise SafetyBlocked(f"response blocked ({finish_name})")
        text = getattr(response, "text", None)
        if not candidates or not text:
            raise EmptyResponse("provider returned no text")
        return GenerateResponse(
            text=text,
            usage=_usage(getattr(response, "usage_metadata", None)),
            model_version=getattr(response, "model_version", None),
            finish_reason=finish_name,
        )

    async def embed(self, request: EmbedRequest) -> EmbedResponse:
        config = genai_types.EmbedContentConfig(
            output_dimensionality=request.output_dimensionality, task_type=request.task_type
        )
        try:
            async with asyncio.timeout(self._timeout_s):
                response = await self._client.aio.models.embed_content(
                    model=request.model, contents=list(request.texts), config=config
                )
        except Exception as exc:
            raise _map_error(exc) from exc
        embeddings = getattr(response, "embeddings", None) or []
        if len(embeddings) != len(request.texts):
            raise EmptyResponse("provider returned a different number of embeddings")
        vectors = tuple(tuple(float(x) for x in (e.values or ())) for e in embeddings)
        if any(not v for v in vectors):
            raise EmptyResponse("provider returned an empty embedding")
        estimated = sum(math.ceil(len(t) / 4) for t in request.texts)
        return EmbedResponse(vectors=vectors, usage=Usage(input_tokens=estimated))

    async def upload_file(self, path: Path, *, mime_type: str) -> FileRef:
        try:
            uploaded = await self._client.aio.files.upload(
                file=str(path), config=genai_types.UploadFileConfig(mime_type=mime_type)
            )
        except Exception as exc:
            raise _map_error(exc) from exc
        if not uploaded.name or not uploaded.uri:
            raise EmptyResponse("file upload returned no name or URI")
        return FileRef(name=uploaded.name, uri=uploaded.uri, mime_type=uploaded.mime_type or mime_type)

    async def delete_file(self, ref: FileRef) -> None:
        try:
            await self._client.aio.files.delete(name=ref.name)
        except Exception as exc:
            raise _map_error(exc) from exc

    async def model_exists(self, model_id: str) -> bool:
        """``models.get``: True if the model ID resolves, False on 404 (smoke test, Q8)."""
        try:
            async with asyncio.timeout(self._timeout_s):
                model = await self._client.aio.models.get(model=model_id)
        except genai_errors.ClientError as exc:
            if exc.code == 404:
                return False
            raise _map_error(exc) from exc
        except Exception as exc:
            raise _map_error(exc) from exc
        return bool(getattr(model, "name", None))
