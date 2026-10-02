"""``AIClient``: the single entry point for model calls (BACKEND_DESIGN.md §5.4-§5.5).

One call = registry lookup (unknown and disabled roles fail first) → request built from the
role's settings → cassette replay or provider call → cost from the price table → one content-
free ``ai_calls`` row (live and record modes only) → JSON parsed and validated against the
output schema. Retry and fallback decisions belong to the caller (``attempts``).
"""

from __future__ import annotations

import datetime
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Generic, Protocol, TypeVar
from uuid import UUID

from pydantic import BaseModel, ValidationError

from eca.intelligence.provider.cassette import CassetteKey, CassetteMode, CassetteStore, input_hash
from eca.intelligence.provider.meter import CallRecord, Meter
from eca.intelligence.provider.pricing import PriceTable
from eca.intelligence.provider.registry import RoleRegistry, RoleSpec
from eca.intelligence.provider.types import (
    AIError,
    CallStatus,
    EmbedRequest,
    EmbedResponse,
    FileRef,
    GenerateRequest,
    GenerateResponse,
    NoFallback,
    ProviderError,
    SchemaInvalid,
    Usage,
)

OutputT = TypeVar("OutputT", bound=BaseModel)


class Provider(Protocol):
    async def generate(self, request: GenerateRequest) -> GenerateResponse: ...

    async def embed(self, request: EmbedRequest) -> EmbedResponse: ...


@dataclass(frozen=True)
class GenerateResult(Generic[OutputT]):
    output: OutputT
    model: str
    usage: Usage
    est_cost_usd: Decimal
    latency_ms: int
    from_cassette: bool
    call_id: UUID | None
    model_version: str | None = None  # the provider's resolved model version, when reported


@dataclass(frozen=True)
class EmbedResult:
    vectors: tuple[tuple[float, ...], ...]
    model: str
    usage: Usage
    est_cost_usd: Decimal
    from_cassette: bool
    call_id: UUID | None


def _validation_summary(exc: ValidationError) -> str:
    """Field paths and error types only (no values: they could contain content)."""
    parts = [f"{'.'.join(str(p) for p in e['loc']) or '<root>'}:{e['type']}" for e in exc.errors()[:10]]
    return "; ".join(parts)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class AIClient:
    def __init__(
        self,
        *,
        registry: RoleRegistry,
        prices: PriceTable,
        mode: CassetteMode,
        provider: Provider | None = None,
        cassettes: CassetteStore | None = None,
        meter: Meter | None = None,
        clock: Callable[[], datetime.datetime] = _utcnow,
    ) -> None:
        if mode in ("live", "record") and provider is None:
            raise AIError(f"mode {mode!r} needs a provider")
        if mode in ("replay", "record") and cassettes is None:
            raise AIError(f"mode {mode!r} needs a cassette store")
        self.registry = registry
        self.prices = prices
        self.mode = mode
        self._provider = provider
        self._cassettes = cassettes
        self._meter = meter
        self._clock = clock

    def _model(self, spec: RoleSpec, use_fallback: bool) -> str:
        if not use_fallback:
            return spec.model
        if spec.fallback is None:
            raise NoFallback(f"AI role {spec.name!r} has no fallback model")
        return spec.fallback

    async def generate(
        self,
        role: str,
        *,
        prompt_version: str,
        schema_version: str,
        output_model: type[OutputT],
        contents: Sequence[str | FileRef],
        system_instruction: str | None = None,
        user_id: UUID | None,
        attempt: int = 1,
        use_fallback: bool = False,
        repair_note: str | None = None,
    ) -> GenerateResult[OutputT]:
        """One structured-output call for ``role``. Raises ``ProviderError`` or ``SchemaInvalid``."""
        spec = self.registry.role(role)
        if spec.is_embedding:
            raise AIError(f"AI role {role!r} is an embedding role")
        model = self._model(spec, use_fallback)
        parts: tuple[str | FileRef, ...] = tuple(contents)
        if repair_note is not None:
            parts += (f"The previous response did not match the required JSON schema: {repair_note}",)
        request = GenerateRequest(
            model=model,
            contents=parts,
            system_instruction=system_instruction,
            response_json_schema=output_model.model_json_schema(),
            temperature=spec.temperature,
            max_output_tokens=spec.max_output_tokens,
            thinking=spec.thinking,
        )
        key = CassetteKey(role=role, prompt_version=prompt_version, input_hash=input_hash(request))
        record = _RecordArgs(spec, model, prompt_version, schema_version, user_id, attempt, use_fallback)

        if self.mode == "replay":
            assert self._cassettes is not None
            response = self._cassettes.get_generate(key)
            latency_ms, from_cassette = 0, True
        else:
            assert self._provider is not None
            started = time.monotonic()
            try:
                response = await self._provider.generate(request)
            except ProviderError as exc:
                await self._meter_call(record, Usage(), _elapsed_ms(started), exc.status, type(exc).__name__)
                raise
            latency_ms, from_cassette = _elapsed_ms(started), False
            if self.mode == "record":
                assert self._cassettes is not None
                self._cassettes.put_generate(key, model, response)

        cost = self.prices.cost(model, response.usage, day=self._clock().date())
        try:
            output = output_model.model_validate(json.loads(response.text))
        except (json.JSONDecodeError, ValidationError) as exc:
            summary = "invalid JSON" if isinstance(exc, json.JSONDecodeError) else _validation_summary(exc)
            if not from_cassette:
                await self._meter_call(
                    record, response.usage, latency_ms, CallStatus.SCHEMA_INVALID, "schema_invalid", cost
                )
            raise SchemaInvalid(summary) from exc
        call_id = None
        if not from_cassette:
            call_id = await self._meter_call(record, response.usage, latency_ms, CallStatus.OK, None, cost)
        return GenerateResult(
            output, model, response.usage, cost, latency_ms, from_cassette, call_id, response.model_version
        )

    async def embed(
        self,
        role: str,
        texts: Sequence[str],
        *,
        prompt_version: str = "none",
        user_id: UUID | None,
        attempt: int = 1,
        task_type: str | None = None,
    ) -> EmbedResult:
        spec = self.registry.role(role)
        if not spec.is_embedding:
            raise AIError(f"AI role {role!r} is not an embedding role")
        request = EmbedRequest(
            model=spec.model,
            texts=tuple(texts),
            output_dimensionality=spec.output_dimensionality,
            task_type=task_type,
        )
        key = CassetteKey(role=role, prompt_version=prompt_version, input_hash=input_hash(request))
        record = _RecordArgs(spec, spec.model, None, None, user_id, attempt, False)
        if self.mode == "replay":
            assert self._cassettes is not None
            response = self._cassettes.get_embed(key)
            latency_ms, from_cassette = 0, True
        else:
            assert self._provider is not None
            started = time.monotonic()
            try:
                response = await self._provider.embed(request)
            except ProviderError as exc:
                await self._meter_call(record, Usage(), _elapsed_ms(started), exc.status, type(exc).__name__)
                raise
            latency_ms, from_cassette = _elapsed_ms(started), False
            if self.mode == "record":
                assert self._cassettes is not None
                self._cassettes.put_embed(key, spec.model, response)
        if spec.output_dimensionality and any(len(v) != spec.output_dimensionality for v in response.vectors):
            raise SchemaInvalid(f"embedding dimension differs from {spec.output_dimensionality}")
        cost = self.prices.cost(spec.model, response.usage, day=self._clock().date())
        call_id = None
        if not from_cassette:
            call_id = await self._meter_call(record, response.usage, latency_ms, CallStatus.OK, None, cost)
        return EmbedResult(response.vectors, spec.model, response.usage, cost, from_cassette, call_id)

    async def _meter_call(
        self,
        args: _RecordArgs,
        usage: Usage,
        latency_ms: int,
        status: CallStatus,
        error_code: str | None,
        cost: Decimal | None = None,
    ) -> UUID | None:
        if self._meter is None:
            return None
        if cost is None:
            cost = self.prices.cost(args.model, usage, day=self._clock().date())
        return await self._meter.record(
            CallRecord(
                user_id=args.user_id,
                role=args.spec.name,
                inventory_id=args.spec.inventory_id,
                model=args.model,
                prompt_version=args.prompt_version,
                schema_version=args.schema_version,
                usage=usage,
                latency_ms=latency_ms,
                est_cost_usd=cost,
                status=status,
                attempt=args.attempt,
                is_fallback=args.is_fallback,
                error_code=error_code,
            )
        )


@dataclass(frozen=True)
class _RecordArgs:
    spec: RoleSpec
    model: str
    prompt_version: str | None
    schema_version: str | None
    user_id: UUID | None
    attempt: int
    is_fallback: bool


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
