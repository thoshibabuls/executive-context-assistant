"""Pydantic output schemas per AI role (AI_PIPELINE.md §11)."""

from eca.intelligence.output_schemas.adjudicate import Adjudication
from eca.intelligence.output_schemas.answer_lookup import Answer, AnswerClaim
from eca.intelligence.output_schemas.email_extract import (
    DecisionOut,
    EmailExtraction,
    Mention,
    Statement,
    StatusSignal,
    Triage,
)
from eca.intelligence.output_schemas.plan_query import PlanQuery
from eca.intelligence.output_schemas.reply_guidance import ReplyGuidance
from eca.intelligence.output_schemas.thread_summary import KeyPoint, ThreadSummary
from eca.intelligence.output_schemas.transcribe import SegmentOut, Transcription

__all__ = [
    "Adjudication",
    "Answer",
    "AnswerClaim",
    "DecisionOut",
    "EmailExtraction",
    "KeyPoint",
    "Mention",
    "PlanQuery",
    "ReplyGuidance",
    "SegmentOut",
    "Statement",
    "StatusSignal",
    "ThreadSummary",
    "Transcription",
    "Triage",
]
