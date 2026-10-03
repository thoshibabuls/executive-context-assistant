"""Slice 0.4 units without a database or network: registry, pricing, cassettes, attempts, client."""

from __future__ import annotations

import datetime
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from eca.intelligence import (
    Action,
    AIClient,
    CassetteKey,
    CassetteMiss,
    CassetteStore,
    ConfigError,
    Degraded,
    ErrorKind,
    NoFallback,
    PriceNotFound,
    PriceTable,
    RoleDisabled,
    RoleRegistry,
    SchemaInvalid,
    UnknownRole,
    Usage,
    classify,
    input_hash,
    load_ai_config,
    next_background_attempt,
    run_interactive,
)
from eca.intelligence.provider.meter import CallRecord, bucket_floor
from eca.intelligence.provider.types import (
    EmbedRequest,
    EmbedResponse,
    EmptyResponse,
    GenerateRequest,
    GenerateResponse,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    SafetyBlocked,
)

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"
DAY = datetime.date(2026, 10, 2)


class Answer(BaseModel):
    reply: str


@pytest.fixture(scope="module")
def config() -> Any:
    return load_ai_config(REPO_CONFIG, today=DAY)


# ---------------------------------------------------------------- registry


def test_repository_config_loads_and_every_model_is_priced(config: Any) -> None:
    registry: RoleRegistry = config.registry
    assert {r.name for r in registry.roles()} == {
        "email_extract",
        "adjudicate",
        "thread_summary",
        "embed",
        "plan_query",
        "answer_lookup",
        "answer_synthesis",
        "reply_guidance",
        "transcribe",
        "meeting_extract",
        "meeting_asks",
        "judge",
    }
    assert registry.verified is False  # no live smoke test has run yet
    assert registry.role("email_extract").inventory_id == "AI-01"
    assert registry.role("embed").output_dimensionality == 768
    assert registry.role("reply_guidance").temperature == 0.3


def test_unknown_and_disabled_roles_fail_before_any_call(config: Any) -> None:
    with pytest.raises(UnknownRole):
        config.registry.role("summarize_everything")
    with pytest.raises(RoleDisabled):
        config.registry.role("adjudicate")  # AI-02 disabled until X1


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_invalid_registry_is_a_config_error(tmp_path: Path) -> None:
    bad = "schema_version: 1\nverified: false\nroles:\n  x: {inventory_id: AI-1, model: m}\n"
    with pytest.raises(ConfigError):
        RoleRegistry.from_file(_write(tmp_path, "models.yaml", bad))
    dup = (
        "schema_version: 1\nverified: false\nroles:\n"
        "  a: {inventory_id: AI-01, model: m}\n  b: {inventory_id: AI-01, model: m}\n"
    )
    with pytest.raises(ConfigError, match="unique"):
        RoleRegistry.from_file(_write(tmp_path, "models.yaml", dup))
    with pytest.raises(ConfigError):
        RoleRegistry.from_file(tmp_path / "missing.yaml")


def test_registry_models_without_price_fail_at_startup(tmp_path: Path) -> None:
    (tmp_path / "models.yaml").write_text(
        "schema_version: 1\nverified: false\nroles:\n  a: {inventory_id: AI-01, model: unpriced-model}\n"
    )
    (tmp_path / "pricing.yaml").write_text(
        "schema_version: 1\ncurrency: USD\nsource: t\nmodels:\n"
        "  other: [{effective_from: 2026-01-01, input_per_mtok: '1'}]\n"
    )
    with pytest.raises(PriceNotFound, match="unpriced-model"):
        load_ai_config(tmp_path, today=DAY)


# ---------------------------------------------------------------- pricing


def test_price_selection_by_effective_date(config: Any) -> None:
    prices: PriceTable = config.prices
    promo = prices.entry("gemini-3.8-flash", datetime.date(2026, 12, 31))
    listed = prices.entry("gemini-3.8-flash", datetime.date(2027, 1, 1))  # effective_to is exclusive
    assert (promo.input_per_mtok, promo.output_per_mtok) == (Decimal("0.75"), Decimal("3.75"))
    assert (listed.input_per_mtok, listed.output_per_mtok) == (Decimal("1.50"), Decimal("7.50"))
    with pytest.raises(PriceNotFound):
        prices.entry("gemini-3.8-flash", datetime.date(2026, 10, 1))  # before the first entry
    with pytest.raises(PriceNotFound):
        prices.entry("no-such-model", DAY)


def test_cost_arithmetic_matches_the_cost_model(config: Any) -> None:
    prices: PriceTable = config.prices
    # AI-01 unit cost (AI_COST_MODEL.md §3): 2,000 in + 325 out (incl. thinking) on T1 = $0.00099
    ai01 = prices.cost(
        "gemini-3.1-flash-lite", Usage(input_tokens=2000, output_tokens=300, thinking_tokens=25), day=DAY
    )
    assert ai01 == Decimal("0.00098750")
    # AI-07 at post-promotion prices: 8,000 in + 1,400 out = $0.0225
    ai07 = prices.cost(
        "gemini-3.8-flash", Usage(input_tokens=8000, output_tokens=1400), day=datetime.date(2027, 2, 1)
    )
    assert ai07 == Decimal("0.02250000")
    # Transcription: one audio hour = $0.30
    assert prices.cost("gemini-3.5-transcribe", Usage(audio_seconds=3600), day=DAY) == Decimal("0.30000000")
    # Audio input tokens on Flash-Lite use the audio price
    audio = prices.cost("gemini-3.1-flash-lite", Usage(input_tokens=1000, audio_input_tokens=1000), day=DAY)
    assert audio == Decimal("0.00050000")
    # Cached input is not discounted (AI_COST_MODEL.md §1)
    cached = prices.cost("gemini-3.1-flash-lite", Usage(input_tokens=1000, cached_input_tokens=800), day=DAY)
    assert cached == Decimal("0.00025000")
    # Embeddings: 1,000 tokens = $0.0002
    assert prices.cost("gemini-embedding-2", Usage(input_tokens=1000), day=DAY) == Decimal("0.00020000")


def test_overlapping_price_entries_are_rejected(tmp_path: Path) -> None:
    body = (
        "schema_version: 1\ncurrency: USD\nsource: t\nmodels:\n  m:\n"
        "    - {effective_from: 2026-01-01, effective_to: 2026-06-01, input_per_mtok: '1'}\n"
        "    - {effective_from: 2026-05-01, input_per_mtok: '2'}\n"
    )
    with pytest.raises(ConfigError, match="overlapping"):
        PriceTable.from_file(_write(tmp_path, "pricing.yaml", body))


# ---------------------------------------------------------------- cassettes


def _request(**overrides: Any) -> GenerateRequest:
    base: dict[str, Any] = {
        "model": "gemini-3.1-flash-lite",
        "contents": ("Synthetic input",),
        "system_instruction": "Return JSON.",
        "response_json_schema": Answer.model_json_schema(),
        "temperature": 0.0,
        "max_output_tokens": 128,
        "thinking": "minimal",
    }
    return GenerateRequest(**{**base, **overrides})


def test_input_hash_is_deterministic_and_covers_the_whole_request() -> None:
    assert input_hash(_request()) == input_hash(_request())
    variants = [
        {"model": "gemini-3.5-flash-lite"},
        {"contents": ("Other input",)},
        {"system_instruction": "Different instructions."},
        {"temperature": 0.3},
        {"max_output_tokens": 256},
        {"thinking": "low"},
        {"response_json_schema": {"type": "object"}},
    ]
    hashes = {input_hash(_request(**v)) for v in variants}
    assert len(hashes) == len(variants) and input_hash(_request()) not in hashes


def test_cassette_round_trip_and_miss(tmp_path: Path) -> None:
    store = CassetteStore(tmp_path)
    key = CassetteKey("plan_query", "v1", input_hash(_request()))
    with pytest.raises(CassetteMiss):
        store.get_generate(key)
    response = GenerateResponse(text='{"reply": "pong"}', usage=Usage(input_tokens=10, output_tokens=3))
    path = store.put_generate(key, "gemini-3.1-flash-lite", response)
    assert path == tmp_path / "plan_query" / "v1" / f"{key.input_hash}.json"
    assert store.get_generate(key) == response
    other = CassetteKey("plan_query", "v2", key.input_hash)  # prompt version is part of the key
    with pytest.raises(CassetteMiss):
        store.get_generate(other)


def test_tampered_cassette_is_a_miss(tmp_path: Path) -> None:
    store = CassetteStore(tmp_path)
    key = CassetteKey("plan_query", "v1", input_hash(_request()))
    path = store.put_generate(key, "m", GenerateResponse(text="{}", usage=Usage()))
    data = json.loads(path.read_text())
    data["input_hash"] = "0" * 64
    path.write_text(json.dumps(data))
    with pytest.raises(CassetteMiss, match="does not match"):
        store.get_generate(key)


@pytest.mark.parametrize("segment", ["../escape", "a/b", "", "..", "."])
def test_cassette_keys_reject_unsafe_segments(segment: str) -> None:
    with pytest.raises(ValueError):
        CassetteKey(segment, "v1", "a" * 64)


@pytest.mark.parametrize("version", ["../v1", "email_extract/..", "a//v1", "/v1", "v1/", "a/b c"])
def test_cassette_keys_reject_unsafe_prompt_versions(version: str) -> None:
    with pytest.raises(ValueError):
        CassetteKey("extract", version, "a" * 64)


def test_every_production_prompt_version_is_a_valid_cassette_key(tmp_path: Path) -> None:
    """Regression: the "<prompt>/<version>" format of every AI role used to fail the key check, so
    every real call raised before reaching the provider."""
    from eca.intelligence import answering, embedding, extraction, meeting_extraction, transcription

    versions = [
        answering.PLAN_PROMPT_VERSION,
        answering.LOOKUP_PROMPT_VERSION,
        answering.SYNTHESIS_PROMPT_VERSION,
        answering.THREAD_SUMMARY_PROMPT_VERSION,
        answering.REPLY_GUIDANCE_PROMPT_VERSION,
        answering.MEETING_ASKS_PROMPT_VERSION,
        embedding.EMBED_INPUT_VERSION,
        extraction.EMAIL_PROMPT_VERSION,
        extraction.ADJ_PROMPT_VERSION,
        meeting_extraction.MEETING_PROMPT_VERSION,
        transcription.TRANSCRIBE_PROMPT_VERSION,
    ]
    store = CassetteStore(tmp_path)
    for version in versions:
        key = CassetteKey("role", version, "b" * 64)
        path = store.put_generate(key, "m", GenerateResponse(text="{}", usage=Usage()))
        assert path == tmp_path / "role" / Path(*version.split("/")) / f"{'b' * 64}.json"
        assert store.get_generate(key).text == "{}"


# ---------------------------------------------------------------- attempt caps


def test_background_attempt_policy() -> None:
    nb = next_background_attempt
    assert nb(ErrorKind.TIMEOUT, calls_made=1, repairs_made=0).action is Action.RETRY
    assert nb(ErrorKind.TIMEOUT, calls_made=1, repairs_made=0).use_fallback is False
    assert nb(ErrorKind.RATE_LIMITED, calls_made=2, repairs_made=0).use_fallback is True  # attempt 3
    assert nb(ErrorKind.PROVIDER_ERROR, calls_made=4, repairs_made=0).action is Action.FAIL_PERMANENT  # cap
    assert nb(ErrorKind.SCHEMA_INVALID, calls_made=1, repairs_made=0).action is Action.REPAIR
    assert nb(ErrorKind.SCHEMA_INVALID, calls_made=2, repairs_made=1).action is Action.FAIL_PERMANENT
    assert nb(ErrorKind.SAFETY_BLOCK, calls_made=1, repairs_made=0).action is Action.FAIL_PERMANENT
    assert nb(ErrorKind.EMPTY_RESPONSE, calls_made=1, repairs_made=0).action is Action.FAIL_PERMANENT
    budget = nb(ErrorKind.BUDGET_EXCEEDED, calls_made=4, repairs_made=0)
    assert (budget.action, budget.counts_toward_cap) == (Action.DEFER, False)
    with pytest.raises(ValueError):
        nb(ErrorKind.TIMEOUT, calls_made=0, repairs_made=0)


def test_error_classification() -> None:
    assert classify(ProviderTimeout()) is ErrorKind.TIMEOUT
    assert classify(ProviderRateLimited()) is ErrorKind.RATE_LIMITED
    assert classify(ProviderUnavailable()) is ErrorKind.PROVIDER_ERROR
    assert classify(SafetyBlocked()) is ErrorKind.SAFETY_BLOCK
    assert classify(EmptyResponse()) is ErrorKind.EMPTY_RESPONSE
    assert classify(SchemaInvalid("x")) is ErrorKind.SCHEMA_INVALID
    with pytest.raises(TypeError):
        classify(RuntimeError())


async def test_interactive_policy_retries_then_falls_back_then_degrades() -> None:
    calls: list[bool] = []

    async def always_times_out(use_fallback: bool) -> str:
        calls.append(use_fallback)
        raise ProviderTimeout()

    with pytest.raises(Degraded) as exc_info:
        await run_interactive(always_times_out)
    assert calls == [False, False, True]
    assert exc_info.value.kind is ErrorKind.TIMEOUT

    calls.clear()

    async def fallback_works(use_fallback: bool) -> str:
        calls.append(use_fallback)
        if not use_fallback:
            raise ProviderUnavailable()
        return "ok"

    assert await run_interactive(fallback_works) == "ok"
    assert calls == [False, False, True]


async def test_interactive_policy_degrades_at_once_on_safety_block() -> None:
    calls = 0

    async def blocked(use_fallback: bool) -> str:
        nonlocal calls
        calls += 1
        raise SafetyBlocked()

    with pytest.raises(Degraded):
        await run_interactive(blocked)
    assert calls == 1


# ---------------------------------------------------------------- client


class FakeProvider:
    def __init__(self, responses: list[GenerateResponse | Exception]) -> None:
        self.responses = responses
        self.requests: list[GenerateRequest] = []

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def embed(self, request: EmbedRequest) -> EmbedResponse:
        return EmbedResponse(vectors=tuple((0.1,) * 768 for _ in request.texts), usage=Usage(input_tokens=4))


class FakeMeter:
    def __init__(self) -> None:
        self.records: list[CallRecord] = []

    async def record(self, rec: CallRecord) -> Any:
        self.records.append(rec)
        return None


def _client(config: Any, mode: str, provider: Any = None, store: Any = None, meter: Any = None) -> AIClient:
    return AIClient(
        registry=config.registry,
        prices=config.prices,
        mode=mode,  # type: ignore[arg-type]
        provider=provider,
        cassettes=store,
        meter=meter,
        clock=lambda: datetime.datetime(2026, 10, 2, 12, tzinfo=datetime.UTC),
    )


async def _call(client: AIClient, **kw: Any) -> Any:
    return await client.generate(
        "plan_query",
        prompt_version="v1",
        schema_version="1",
        output_model=Answer,
        contents=["Synthetic question"],
        user_id=None,
        **kw,
    )


async def test_live_call_builds_request_from_role_and_meters_without_content(config: Any) -> None:
    provider = FakeProvider(
        [GenerateResponse(text='{"reply": "pong"}', usage=Usage(input_tokens=1500, output_tokens=200))]
    )
    meter = FakeMeter()
    result = await _call(_client(config, "live", provider, meter=meter))
    assert result.output == Answer(reply="pong") and result.from_cassette is False
    [request] = provider.requests
    assert (request.model, request.thinking, request.temperature) == ("gemini-3.1-flash-lite", "minimal", 0.0)
    assert request.response_json_schema == Answer.model_json_schema()
    [rec] = meter.records
    assert (rec.role, rec.inventory_id, rec.status.value, rec.attempt) == ("plan_query", "AI-05", "ok", 1)
    assert rec.est_cost_usd == Decimal("0.00067500")  # AI_COST_MODEL.md §3: AI-05 ≈ $0.00068
    assert "pong" not in repr(rec) and "Synthetic" not in repr(rec)  # content-free


async def test_fallback_uses_the_fallback_model(config: Any) -> None:
    provider = FakeProvider([GenerateResponse(text='{"reply": "x"}', usage=Usage())])
    meter = FakeMeter()
    result = await _call(_client(config, "live", provider, meter=meter), use_fallback=True, attempt=3)
    assert result.model == "gemini-3.5-flash-lite"
    assert meter.records[0].is_fallback is True and meter.records[0].attempt == 3


async def test_role_without_fallback_raises_no_fallback(config: Any) -> None:
    from eca.intelligence import RoleSpec

    registry = RoleRegistry(
        {"solo": RoleSpec(inventory_id="AI-90", model="gemini-3.1-flash-lite")}, verified=False
    )
    provider = FakeProvider([])
    client = AIClient(registry=registry, prices=config.prices, mode="live", provider=provider)
    with pytest.raises(NoFallback):
        await client.generate(
            "solo",
            prompt_version="v1",
            schema_version="1",
            output_model=Answer,
            contents=["x"],
            user_id=None,
            use_fallback=True,
        )
    assert provider.requests == []


@pytest.mark.parametrize(
    "error, status",
    [
        (ProviderTimeout(), "timeout"),
        (ProviderRateLimited(code=429), "rate_limited"),
        (ProviderUnavailable(code=503), "provider_error"),
        (SafetyBlocked(), "safety_block"),
        (EmptyResponse(), "empty_response"),
    ],
)
async def test_provider_failures_are_metered_and_raised(config: Any, error: Exception, status: str) -> None:
    meter = FakeMeter()
    with pytest.raises(type(error)):
        await _call(_client(config, "live", FakeProvider([error]), meter=meter))
    [rec] = meter.records
    assert rec.status.value == status and rec.est_cost_usd == Decimal("0E-8")


@pytest.mark.parametrize("text", ["not json", '{"unexpected": 1}', "[]"])
async def test_malformed_structured_output_is_schema_invalid(config: Any, text: str) -> None:
    meter = FakeMeter()
    with pytest.raises(SchemaInvalid) as exc_info:
        await _call(
            _client(config, "live", FakeProvider([GenerateResponse(text=text, usage=Usage())]), meter=meter)
        )
    assert "unexpected" not in exc_info.value.summary or "1" not in exc_info.value.summary
    assert meter.records[0].status.value == "schema_invalid"


async def test_repair_note_is_added_to_the_request(config: Any) -> None:
    provider = FakeProvider([GenerateResponse(text='{"reply": "x"}', usage=Usage())])
    await _call(_client(config, "live", provider), repair_note="reply:missing")
    assert provider.requests[0].contents[-1].endswith("reply:missing")


async def test_disabled_and_unknown_roles_never_reach_the_provider(config: Any) -> None:
    provider = FakeProvider([])
    client = _client(config, "live", provider)
    with pytest.raises(RoleDisabled):
        await client.generate(
            "adjudicate",
            prompt_version="v1",
            schema_version="1",
            output_model=Answer,
            contents=["x"],
            user_id=None,
        )
    with pytest.raises(UnknownRole):
        await client.generate(
            "nope", prompt_version="v1", schema_version="1", output_model=Answer, contents=["x"], user_id=None
        )
    assert provider.requests == []


async def test_record_then_replay_is_deterministic_and_offline(config: Any, tmp_path: Path) -> None:
    store = CassetteStore(tmp_path)
    provider = FakeProvider([GenerateResponse(text='{"reply": "recorded"}', usage=Usage(input_tokens=7))])
    recorded = await _call(_client(config, "record", provider, store))
    assert recorded.from_cassette is False
    meter = FakeMeter()
    replay = _client(config, "replay", None, store, meter=meter)  # no provider at all
    first, second = await _call(replay), await _call(replay)
    assert first.output == second.output == recorded.output
    assert first.est_cost_usd == second.est_cost_usd == recorded.est_cost_usd
    assert first.from_cassette and meter.records == []  # replay is never metered
    with pytest.raises(CassetteMiss):
        await replay.generate(
            "plan_query",
            prompt_version="v2",
            schema_version="1",
            output_model=Answer,
            contents=["Synthetic question"],
            user_id=None,
        )


async def test_embed_checks_dimensions_and_role_kind(config: Any) -> None:
    client = _client(config, "live", FakeProvider([]), meter=FakeMeter())
    result = await client.embed("embed", ["alpha", "beta"], user_id=None)
    assert len(result.vectors) == 2 and len(result.vectors[0]) == 768
    with pytest.raises(Exception, match="not an embedding role"):
        await client.embed("plan_query", ["x"], user_id=None)


def test_client_mode_requirements(config: Any) -> None:
    with pytest.raises(Exception, match="needs a provider"):
        _client(config, "live")
    with pytest.raises(Exception, match="needs a cassette store"):
        _client(config, "replay")


def test_bucket_floor() -> None:
    t = datetime.datetime(2026, 10, 2, 12, 44, 59, tzinfo=datetime.UTC)
    assert bucket_floor(t) == datetime.datetime(2026, 10, 2, 12, 30, tzinfo=datetime.UTC)
    with pytest.raises(ValueError):
        bucket_floor(datetime.datetime(2026, 10, 2))
