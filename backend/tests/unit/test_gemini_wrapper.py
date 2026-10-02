"""Gemini wrapper with real ``google-genai`` types and a fake transport (no network)."""

from __future__ import annotations

import asyncio
import datetime
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import SecretStr, ValidationError

from eca.intelligence import EvidenceSpan, Provenance, SourceRef, confidence_band
from eca.intelligence.provider.gemini import GeminiProvider
from eca.intelligence.provider.types import (
    EmbedRequest,
    EmptyResponse,
    FileRef,
    GenerateRequest,
    ProviderRateLimited,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
    SafetyBlocked,
)


def _response(text: str | None = '{"reply": "ok"}', finish: str = "STOP", **usage: int) -> Any:
    parts = [genai_types.Part(text=text)] if text is not None else []
    return genai_types.GenerateContentResponse(
        candidates=[
            genai_types.Candidate(
                content=genai_types.Content(role="model", parts=parts),
                finish_reason=genai_types.FinishReason(finish),
            )
        ],
        usage_metadata=genai_types.GenerateContentResponseUsageMetadata(
            prompt_token_count=usage.get("prompt", 100),
            cached_content_token_count=usage.get("cached", 0),
            candidates_token_count=usage.get("out", 20),
            thoughts_token_count=usage.get("thoughts", 5),
            prompt_tokens_details=[
                genai_types.ModalityTokenCount(
                    modality=genai_types.MediaModality.AUDIO, token_count=usage.get("audio", 0)
                )
            ],
        ),
        model_version="test-model-001",
    )


class FakeModels:
    def __init__(self, result: Any = None, error: BaseException | None = None, delay: float = 0.0) -> None:
        self.result, self.error, self.delay = result, error, delay
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> Any:
        return await self._answer(kwargs)

    async def embed_content(self, **kwargs: Any) -> Any:
        return await self._answer(kwargs)

    async def get(self, **kwargs: Any) -> Any:
        return await self._answer(kwargs)

    async def _answer(self, kwargs: dict[str, Any]) -> Any:
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result


def _provider(models: FakeModels, timeout_s: float = 5.0) -> GeminiProvider:
    client = SimpleNamespace(aio=SimpleNamespace(models=models, files=SimpleNamespace()))
    return GeminiProvider(None, timeout_s=timeout_s, client=client)


REQUEST = GenerateRequest(
    model="gemini-3.1-flash-lite",
    contents=("Synthetic text", FileRef(name="files/a", uri="https://files.test/a", mime_type="audio/ogg")),
    system_instruction="Return JSON.",
    response_json_schema={"type": "object", "properties": {"reply": {"type": "string"}}},
    temperature=0.0,
    max_output_tokens=64,
    thinking="minimal",
)


async def test_generate_sends_structured_output_config_and_parses_usage() -> None:
    models = FakeModels(_response(prompt=1200, cached=200, out=80, thoughts=15, audio=300))
    result = await _provider(models).generate(REQUEST)
    assert result.text == '{"reply": "ok"}'
    usage = result.usage
    assert (usage.input_tokens, usage.cached_input_tokens, usage.output_tokens) == (1200, 200, 80)
    assert (usage.thinking_tokens, usage.audio_input_tokens) == (15, 300)
    [call] = models.calls
    config: genai_types.GenerateContentConfig = call["config"]
    assert call["model"] == "gemini-3.1-flash-lite"
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == REQUEST.response_json_schema
    assert config.thinking_config is not None
    assert config.thinking_config.thinking_level == genai_types.ThinkingLevel.MINIMAL
    assert (config.temperature, config.max_output_tokens, config.system_instruction) == (
        0.0,
        64,
        "Return JSON.",
    )
    parts = call["contents"][0].parts
    assert parts[0].text == "Synthetic text" and parts[1].file_data.file_uri == "https://files.test/a"


@pytest.mark.parametrize(
    "error, expected",
    [
        (genai_errors.ClientError(429, {"error": {"message": "quota"}}), ProviderRateLimited),
        (genai_errors.ClientError(404, {"error": {"message": "no model"}}), ProviderRejected),
        (genai_errors.ClientError(400, {"error": {"message": "bad"}}), ProviderRejected),
        (genai_errors.ServerError(503, {"error": {"message": "down"}}), ProviderUnavailable),
        (httpx.ReadTimeout("slow"), ProviderTimeout),
        (httpx.ConnectError("refused"), ProviderUnavailable),
    ],
)
async def test_provider_errors_are_mapped(error: BaseException, expected: type[Exception]) -> None:
    with pytest.raises(expected) as exc_info:
        await _provider(FakeModels(error=error)).generate(REQUEST)
    assert "quota" not in str(exc_info.value)  # provider messages are not propagated


async def test_slow_call_times_out() -> None:
    with pytest.raises(ProviderTimeout):
        await _provider(FakeModels(_response(), delay=0.5), timeout_s=0.05).generate(REQUEST)


@pytest.mark.parametrize("finish", ["SAFETY", "PROHIBITED_CONTENT", "RECITATION"])
async def test_safety_finish_reasons_are_blocks(finish: str) -> None:
    with pytest.raises(SafetyBlocked):
        await _provider(FakeModels(_response(finish=finish))).generate(REQUEST)


async def test_prompt_block_and_empty_responses() -> None:
    blocked = genai_types.GenerateContentResponse(
        prompt_feedback=genai_types.GenerateContentResponsePromptFeedback(
            block_reason=genai_types.BlockedReason.SAFETY
        )
    )
    with pytest.raises(SafetyBlocked):
        await _provider(FakeModels(blocked)).generate(REQUEST)
    with pytest.raises(EmptyResponse):
        await _provider(FakeModels(_response(text=None))).generate(REQUEST)
    with pytest.raises(EmptyResponse):
        await _provider(FakeModels(genai_types.GenerateContentResponse(candidates=[]))).generate(REQUEST)


async def test_embed_vectors_and_estimated_usage() -> None:
    response = genai_types.EmbedContentResponse(
        embeddings=[
            genai_types.ContentEmbedding(values=[0.5] * 8),
            genai_types.ContentEmbedding(values=[0.25] * 8),
        ]
    )
    models = FakeModels(response)
    result = await _provider(models).embed(
        EmbedRequest(model="e", texts=("abcd", "abcdefgh"), output_dimensionality=8)
    )
    assert result.vectors == ((0.5,) * 8, (0.25,) * 8)
    assert result.usage.input_tokens == 3  # ceil(4/4) + ceil(8/4)
    assert models.calls[0]["config"].output_dimensionality == 8
    short = genai_types.EmbedContentResponse(embeddings=[genai_types.ContentEmbedding(values=[0.5])])
    with pytest.raises(EmptyResponse):
        await _provider(FakeModels(short)).embed(EmbedRequest(model="e", texts=("a", "b")))


async def test_model_exists_for_smoke_test() -> None:
    assert await _provider(FakeModels(genai_types.Model(name="models/x"))).model_exists("x") is True
    missing = FakeModels(error=genai_errors.ClientError(404, {"error": {"message": "not found"}}))
    assert await _provider(missing).model_exists("y") is False
    with pytest.raises(ProviderUnavailable):
        await _provider(FakeModels(error=genai_errors.ServerError(500, {"error": {}}))).model_exists("z")


def test_live_provider_needs_a_key() -> None:
    with pytest.raises(ProviderRejected, match="GEMINI_API_KEY"):
        GeminiProvider(SecretStr(""), timeout_s=1.0)


# ---------------------------------------------------------------- provenance envelope

NOW = datetime.datetime(2026, 10, 1, 9, 14, 3, tzinfo=datetime.UTC)
SRC = SourceRef(source_item_id=uuid.uuid4(), kind="message", occurred_at=NOW)
EVIDENCE = EvidenceSpan(evidence_id=uuid.uuid4(), quote="I will send it Friday", char_start=10, char_end=31)


def _prov(**kw: Any) -> Provenance:
    base: dict[str, Any] = {
        "source": (SRC,),
        "confidence": 0.86,
        "confidence_band": "high",
        "derived_at": NOW,
        "extraction_method": "llm",
        "model": "gemini-3.1-flash-lite",
        "prompt_version": "email_extract/v1",
        "evidence": (EVIDENCE,),
    }
    return Provenance(**{**base, **kw})


def test_valid_envelopes() -> None:
    assert _prov().confidence_band == "high"
    rule = _prov(
        extraction_method="rule",
        model=None,
        prompt_version=None,
        evidence=(),
        confidence=0.5,
        confidence_band="low",
    )
    assert rule.extraction_method == "rule"


@pytest.mark.parametrize(
    "overrides",
    [
        {"source": ()},  # no source
        {"evidence": ()},  # LLM without evidence
        {"model": None},  # LLM without model
        {"prompt_version": None},  # LLM without prompt version
        {"confidence_band": "medium"},  # band does not match 0.86
        {"confidence": 1.2, "confidence_band": "high"},
        {"derived_at": datetime.datetime(2026, 10, 1, 9)},  # naive time
        {"extraction_method": "rule"},  # rule with model and prompt version
    ],
)
def test_invalid_envelopes_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _prov(**overrides)


def test_confidence_bands() -> None:
    assert [confidence_band(c) for c in (0.0, 0.59, 0.6, 0.79, 0.8, 1.0)] == [
        "low",
        "low",
        "medium",
        "medium",
        "high",
        "high",
    ]


def test_evidence_span_ranges() -> None:
    with pytest.raises(ValidationError):
        EvidenceSpan(evidence_id=uuid.uuid4(), quote="x", char_start=5)
    with pytest.raises(ValidationError):
        EvidenceSpan(evidence_id=uuid.uuid4(), quote="x", char_start=5, char_end=5)
    with pytest.raises(ValidationError):
        EvidenceSpan(evidence_id=uuid.uuid4(), quote="")
