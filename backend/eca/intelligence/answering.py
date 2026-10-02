"""Interactive AI calls of the chat path: AI-05 planner, AI-06 lookups, AI-07 synthesis
(AI_PIPELINE.md §3, §5.7-§5.8, §7, §11).

Each call goes through ``AIClient`` (role registry, cassettes, ``ai_calls`` meter) with the
interactive attempt policy: the primary model, one retry, one fallback call, then degradation.
Nothing here touches the database or decides facts: the caller verifies the answer's claims.
The packet and the question arrive already rendered, with untrusted text delimited.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel

from eca.intelligence.extraction import prompt_text
from eca.intelligence.output_schemas.answer_lookup import SCHEMA_VERSION as ANSWER_SCHEMA
from eca.intelligence.output_schemas.answer_lookup import Answer
from eca.intelligence.output_schemas.plan_query import SCHEMA_VERSION as PLAN_SCHEMA
from eca.intelligence.output_schemas.plan_query import PlanQuery
from eca.intelligence.provider.attempts import Degraded, run_interactive
from eca.intelligence.provider.client import AIClient, GenerateResult
from eca.intelligence.provider.types import AIError
from eca.platform.errors import BudgetExceeded

PLAN_PROMPT_VERSION = "plan_query/v1"
LOOKUP_PROMPT_VERSION = "answer_lookup/v1"
SYNTHESIS_PROMPT_VERSION = "answer_synthesis/v1"

OutT = TypeVar("OutT", bound=BaseModel)


@dataclass(frozen=True)
class InteractiveCall(Generic[OutT]):
    output: OutT | None
    role: str
    model: str | None
    prompt_version: str
    call_ids: tuple[UUID, ...]
    degraded: str | None = None  # error kind when the call gave up (AI_PIPELINE.md §14)


async def _interactive(
    client: AIClient,
    *,
    role: str,
    prompt_version: str,
    schema_version: str,
    output_model: type[OutT],
    content: str,
    user_id: UUID | None,
) -> InteractiveCall[OutT]:
    system = prompt_text(prompt_version)
    attempts = {"n": 0}
    calls: list[UUID] = []

    async def call(use_fallback: bool) -> GenerateResult[OutT]:
        attempts["n"] += 1
        result = await client.generate(
            role,
            prompt_version=prompt_version,
            schema_version=schema_version,
            output_model=output_model,
            contents=[content],
            system_instruction=system,
            user_id=user_id,
            attempt=attempts["n"],
            use_fallback=use_fallback,
        )
        if result.call_id is not None:
            calls.append(result.call_id)
        return result

    try:
        result = await run_interactive(call)
    except Degraded as exc:
        return InteractiveCall(None, role, None, prompt_version, tuple(calls), exc.kind.value)
    except AIError as exc:  # role disabled, unknown role, cassette miss, no fallback
        return InteractiveCall(None, role, None, prompt_version, tuple(calls), type(exc).__name__)
    except BudgetExceeded:  # the guard refused the call (AI_COST_MODEL.md §7.2)
        return InteractiveCall(None, role, None, prompt_version, tuple(calls), "budget_exceeded")
    return InteractiveCall(result.output, role, result.model, prompt_version, tuple(calls))


async def run_plan_query(
    client: AIClient, question_block: str, *, user_id: UUID | None
) -> InteractiveCall[PlanQuery]:
    """AI-05: route a question the rules could not route. ``question_block`` is delimited."""
    return await _interactive(
        client,
        role="plan_query",
        prompt_version=PLAN_PROMPT_VERSION,
        schema_version=PLAN_SCHEMA,
        output_model=PlanQuery,
        content=f"QUESTION:\n{question_block}",
        user_id=user_id,
    )


async def run_answer(
    client: AIClient, packet_text: str, *, synthesis: bool, user_id: UUID | None
) -> InteractiveCall[Answer]:
    """AI-06 (lookup, T1) or AI-07 (synthesis, T2) over a rendered packet (question last)."""
    return await _interactive(
        client,
        role="answer_synthesis" if synthesis else "answer_lookup",
        prompt_version=SYNTHESIS_PROMPT_VERSION if synthesis else LOOKUP_PROMPT_VERSION,
        schema_version=ANSWER_SCHEMA,
        output_model=Answer,
        content=f"PACKET:\n{packet_text}",
        user_id=user_id,
    )
