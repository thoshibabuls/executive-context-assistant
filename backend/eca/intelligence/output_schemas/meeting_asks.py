"""AI-11 ``meeting_asks`` output schema, version 1 (AI_PIPELINE.md §5.10).

3-5 suggested asks for a meeting, each a recommendation that cites the prep sections it rests on
(``S1``...``Sn``). Code drops asks whose citations are missing or unknown; nothing is phrased as
fact.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "meeting_asks.v1"


class Ask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=300)
    citations: list[str] = Field(min_length=1, max_length=6)


class MeetingAsks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asks: list[Ask] = Field(default_factory=list, max_length=5)
    answerable: bool = True
