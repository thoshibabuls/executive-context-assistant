"""Slice 0.4 live smoke test (IMPLEMENTATION_PLAN.md slice 0.4, AI_PIPELINE.md §2, Q8).

Marker ``live``: deselected by default (``-m "not live"`` in pytest's addopts) and never run in
CI. Run it explicitly with a restricted key provided as an environment secret:

    GEMINI_API_KEY=... pytest -m live tests/live

It checks every configured model ID with ``models.get``, makes one structured-output call on a
T1 role and one embedding, all on fixed synthetic text (no user data). It writes the resolved
model IDs to a git-ignored report (``evals/ai/reports/smoke/``) and never prints, logs or stores
the key.
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

from eca.intelligence import AIClient, load_ai_config
from eca.intelligence.provider.gemini import GeminiProvider

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("GEMINI_API_KEY"), reason="GEMINI_API_KEY not set (live smoke test)"
    ),
]

REPORT_DIR = Path(__file__).resolve().parents[3] / "evals" / "ai" / "reports" / "smoke"
SYNTHETIC_TEXT = (
    "Smoke test. Fictional text, no user data: the quarterly report for Example Corp is due Friday."
)


class SmokeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    mentions_deadline: bool


@pytest.fixture(scope="module")
def provider() -> GeminiProvider:
    return GeminiProvider(SecretStr(os.environ["GEMINI_API_KEY"]), timeout_s=60)


async def test_live_smoke(provider: GeminiProvider) -> None:
    config = load_ai_config()
    client = AIClient(
        registry=config.registry,
        prices=config.prices,
        mode="live",
        provider=provider,
        cassettes=None,
        meter=None,
    )

    models = {
        model_id: await provider.model_exists(model_id) for model_id in sorted(config.registry.model_ids())
    }

    generated = await client.generate(
        "plan_query",
        prompt_version="smoke-v1",
        schema_version="smoke-v1",
        output_model=SmokeOutput,
        contents=[f"Answer in one short sentence whether this text mentions a deadline.\n\n{SYNTHETIC_TEXT}"],
        user_id=None,
    )
    embedded = await client.embed("embed", [SYNTHETIC_TEXT], user_id=None)

    report = {
        "ran_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "models": models,
        "generate": {
            "role": "plan_query",
            "model": generated.model,
            "resolved_model_version": generated.model_version,
            "usage": vars(generated.usage),
            "est_cost_usd": str(generated.est_cost_usd),
            "latency_ms": generated.latency_ms,
        },
        "embed": {
            "role": "embed",
            "model": embedded.model,
            "dimensions": len(embedded.vectors[0]),
        },
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    (REPORT_DIR / f"smoke_{stamp}.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )

    missing = sorted(m for m, ok in models.items() if not ok)
    assert not missing, f"configured model IDs that do not resolve: {missing} (update config/models.yaml)"
    assert isinstance(generated.output, SmokeOutput)
    assert generated.usage.input_tokens > 0
    spec = config.registry.role("embed")
    assert len(embedded.vectors) == 1
    assert len(embedded.vectors[0]) == (spec.output_dimensionality or len(embedded.vectors[0]))
